"""The writer follows Claude's usage limit like the other agents: while it holds, no packet is handed to a session and
the page says until when; the hold survives a restart of the app; a packet that no session ever began never blocks its
role: the next Go asks for another. Synthetic data only."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.browser import go as apply_go
from app.jobsearch.missions import catch_up
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.pool import core as pool
from app.jobsearch.research import analysis
from app.jobsearch.work import availability, session_kinds
from app.jobsearch.work import hours as night
from app.jobsearch.writing import packets
from tests.builders import save_documents
from tests.support import OWNER, fake_go_window, load_profile


async def _setup(roles: int = 1) -> list[Opportunity]:
    made = []
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        await save_documents(session)
        await roster.ensure_roster(session, OWNER)
        for i in range(roles):
            role = Opportunity(
                owner=OWNER, slug=f"co-{i}", company=f"Co {i}", title=f"Role {i}", fit=6, origin="scout",
                match_key=f"co {i}|role {i}", status=stages.READY_TO_LAUNCH, found=date.today(),
                analysis_state="done",
                analysis={"required": [{"item": "Programs", "class": "P"}], analysis.DECK_READ: True},
                sourced_at=datetime.now(UTC),
                url=f"https://careers.example.test/open/co-{i}")
            session.add(role)
            made.append(role)
        await session.commit()
    return made


async def _limit(hours: float = 3) -> datetime:
    now = datetime.now(UTC)
    limit = {"resets_at": (now + timedelta(hours=hours)).isoformat(), "text": "You've hit your limit"}
    async with db.session_factory()() as session:
        await catch_up.note_usage_limit(session, OWNER, limit, now=now)
        await session.commit()
    return now + timedelta(hours=hours)


async def _asked(role: Opportunity) -> None:
    """The role's packet is asked for (what Go does for it), so a writer's session has one to be handed."""
    async with db.session_factory()() as session:
        await packets.start(session, OWNER, role.id, by="resumebot")
        await session.commit()


async def _next() -> str | None:
    async with db.session_factory()() as session:
        kind = await session_kinds.next_kind(session, OWNER)
        await session.commit()
    return kind


async def _packets() -> list[Packet]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Packet).order_by(Packet.version))).all())


async def test_no_packet_is_handed_to_a_session_while_the_usage_limit_holds_and_the_page_says_until_when(js):
    (role,) = await _setup()
    await _asked(role)
    lifts = await _limit()
    assert await _next() is None
    async with db.session_factory()() as session:
        assert (await availability.barred(session, OWNER, availability.Question.POOL)).words == "Claude's usage limit"
        lines = {k["kind"]: k["health"] for k in await pool.listing(session, OWNER)}
    assert night.when_words(lifts) in lines[pool.WRITER] and "usage limit" in lines[pool.WRITER]


async def test_the_usage_limit_hold_survives_a_restart_of_the_app(js):
    (role,) = await _setup()
    await _asked(role)
    lifts = await _limit()
    availability.forget()  # a fresh process: the app's memory is empty, the database is the same
    async with db.session_factory()() as session:
        held = await availability.limit_until(session, OWNER)
        assert held is not None and abs(held - lifts) < timedelta(seconds=1)
        assert await session_kinds.next_kind(session, OWNER) is None


async def test_with_no_limit_the_asked_packets_seat_is_handed_to_a_writer_session(js):
    (role,) = await _setup()
    await _asked(role)
    assert await _next() == session_kinds.SEAT


async def test_a_packet_no_session_began_never_blocks_its_role_go_asks_for_another(js, monkeypatch):
    """Found with the nightly packets, which counted such a packet as the role's failure for the night: it is swept as
    failed after the limit held, no session ever began it (`attempts` 0), and Go simply asks again."""
    monkeypatch.setattr(apply_go, "go_window", fake_go_window([]))
    (role,) = await _setup()
    async with db.session_factory()() as session:
        for version, hours in ((1, 30), (2, 6)):  # two earlier nights; the usage limit held and no session began them
            session.add(Packet(owner=OWNER, role_id=role.id, version=version, status="failed", attempts=0,
                               created_at=datetime.now(UTC) - timedelta(hours=hours)))
        await session.commit()
    await apply_go.go(OWNER, poll=0, wait=0)
    assert [(p.version, p.status) for p in await _packets()] == [(1, "failed"), (2, "failed"), (3, "writing")]
