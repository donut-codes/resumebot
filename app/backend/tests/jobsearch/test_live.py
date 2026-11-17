"""Live updates (live.py, GET /api/v1/live): every role move, star and packet change is announced to the owner's open
streams when it commits, whichever code made it, with the counts after it; and a move answers with the role's card and
the counts at once. Synthetic roles only."""

import asyncio
import json
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event, func, select, update

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.pipeline import live, live_sink
from tests.support import OWNER


def _role(slug, status=stages.FIRST_IMPRESSION, **extra):
    return Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", status=status,
                       origin="scout", match_key=f"co {slug}|role {slug}", found=date.today(), **extra)


async def _add(*rows) -> list[uuid.UUID]:
    async with db.session_factory()() as session:
        session.add_all(rows)
        await session.commit()
        return [r.id for r in rows]


async def _drain(sub: live.Subscriber, *, counts: bool = False) -> list[tuple[str, dict]]:
    """What is queued now. The owner's counts go out COUNTS_AFTER_SECONDS after a commit, so on a loaded machine one for
    an earlier commit can land between any two steps of a test; they are left out unless a test is about them."""
    await live_sink.settle()
    out = []
    while not sub.queue.empty():
        _, kind, data = sub.queue.get_nowait()
        if kind != "counts" or counts:
            out.append((kind, data))
    return out


@pytest.fixture
def listening(js):
    sub = live.broker.subscribe(OWNER)
    yield sub
    live.broker.unsubscribe(sub)


async def test_a_committed_move_is_announced_with_what_a_row_needs_and_then_the_counts(listening):
    (role_id,) = await _add(_role("a"))
    assert [k for k, _ in await _drain(listening)] == ["role"], "a new role is announced"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.status, role.screened_out_reason = stages.GATED_OUT, "Pay below your floor"
        await session.commit()
    (kind, data), = await _drain(listening)
    assert kind == "role" and data["id"] == str(role_id) and data["changed"] == ["status"]
    assert (data["from"], data["to"], data["lane_from"], data["lane_to"]) == (
        stages.FIRST_IMPRESSION, stages.GATED_OUT, stages.FIRST_IMPRESSION, "closed")
    assert data["closed_reason"] == "gated_out" and data["reason"] == "Pay below your floor"
    assert data["title"] == "Role a" and data["favorite"] is False
    await asyncio.sleep(live.COUNTS_AFTER_SECONDS + 0.3)
    sent = [c for k, c in await _drain(listening, counts=True) if k == "counts"]
    assert sent, "the counts follow the commit"
    counts = sent[-1]  # the last one is the counts after the move, however many commits' worth came before it
    assert counts["lanes"]["closed"] == 1 and counts["lanes"][stages.FIRST_IMPRESSION] == 0
    assert counts["closed"]["gated_out"] == 1 and counts["closed_total"] == 1


async def test_a_change_made_by_a_bulk_update_or_rolled_back_is_not_announced_but_a_star_is(listening):
    (role_id,) = await _add(_role("b"))
    await _drain(listening)
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.status = stages.APPLIED
        await session.flush()
        await session.rollback()
    assert await _drain(listening) == [], "nothing that was rolled back"
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.notes = "a note moves nothing"
        await session.commit()
    assert await _drain(listening) == []
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.favorited_at = datetime.now(UTC)
        await session.commit()
    (kind, data), = [x for x in await _drain(listening) if x[0] == "role"]
    assert data["changed"] == ["favorite"] and data["favorite"] is True and data["from"] == data["to"]


async def test_a_packet_asked_and_moved_on_is_announced(listening):
    (role_id,) = await _add(_role("c", stages.READY_TO_LAUNCH))
    async with db.session_factory()() as session:
        packet = Packet(owner=OWNER, role_id=role_id, version=1, status="writing", feedback="", brief={}, resume={},
                        letter={}, comp={}, reasoning={}, checks=[])
        session.add(packet)
        await session.commit()
        packet_id = packet.id
    await live_sink.settle()
    async with db.session_factory()() as session:
        await session.execute(update(Packet).where(Packet.id == packet_id).values(status="failed"))  # Core: unseen
        await session.commit()
    async with db.session_factory()() as session:
        packet = await session.get(Packet, packet_id)
        packet.status = "ready"
        await session.commit()
    packets = [d for k, d in await _drain(listening) if k == "packet"]
    assert [(p["status"], p["from"]) for p in packets] == [("writing", None), ("ready", "failed")]
    assert packets[0]["role_id"] == str(role_id) and packets[0]["version"] == 1
    assert packets[0]["progress"]["step"] == "asked" and packets[1]["progress"]["step"] == "done"


async def test_a_packet_moving_on_a_step_of_its_way_is_announced_and_a_change_within_a_step_is_not(listening):
    from app.jobsearch.writing import packets as packet_service

    (role_id,) = await _add(_role("h", stages.READY_TO_LAUNCH))
    async with db.session_factory()() as session:
        packet = Packet(owner=OWNER, role_id=role_id, version=1, status="writing", feedback="", brief={}, resume={},
                        letter={}, comp={}, reasoning={}, checks=[])
        session.add(packet)
        await session.commit()
        packet_id = packet.id
    await _drain(listening)

    async def change(**values):
        async with db.session_factory()() as session:
            row = await session.get(Packet, packet_id)
            for name, value in values.items():
                setattr(row, name, value)
            await session.commit()
        await live_sink.settle()

    key = packet_service.STEP_KEY
    await change(brief={key: "checking"})  # the composer's answer is in: the facts gate
    await change(brief={key: "checking", "draft": {"more": "in the same step"}})  # the same step
    await change(brief={key: "fixing"})  # on to the fix
    steps = [(d["progress"]["step"], d["progress"]["done"], d["progress"]["of"]) for k, d in await _drain(listening)
             if k == "packet"]
    assert steps == [("checking", 1, 3), ("fixing", 2, 3)]


def test_a_packets_progress_reads_its_step_from_its_status_and_its_brief():
    from app.jobsearch.writing.packets import progress_of

    assert progress_of("writing", None)["step"] == "asked"
    assert progress_of("writing", "nonsense")["step"] == "asked", "a step the packet does not know is the first"
    assert progress_of("writing", "checking")["fraction"] == 0.33
    assert progress_of("writing", "fixing")["label"] == "Being fixed"
    assert progress_of("held", "checking") == {"step": "done", "label": "Held for you", "done": 3, "of": 3,
                                               "fraction": 1.0}


async def test_a_page_that_falls_too_far_behind_is_told_to_read_the_board_again(listening):
    for n in range(live.MOST_HELD + 5):
        live.broker.publish(OWNER, "role", {"n": n})
    held = await _drain(listening)
    assert held[0][0] == "resync" and len(held) < live.MOST_HELD


async def test_the_counts_say_how_many_closed_roles_each_reason_holds_zero_included(js):
    await _add(
        _role("gate1", stages.GATED_OUT, screened_out_reason="pay"), _role("gate2", stages.GATED_OUT),
        _role("no", stages.REJECTED, ended_reason="they_said_no"),
        _role("gone", stages.WITHDREW, ended_reason="posting_gone"),
        _role("offer", stages.WITHDREW, ended_reason="received_offer"),
        _role("pay", stages.WITHDREW, ended_reason="pay_too_low", target_role="builders"),
        _role("quiet", stages.GONE_QUIET, ended_reason="unknown"), _role("live"))
    async with db.session_factory()() as session:
        counts = await live.counts(session, OWNER)
    assert counts["closed_total"] == 7
    assert counts["closed"] == {
        "gated_out": 2, "expired": 1, "rejected": 1, "received_offer": 1,
        "not_my_field": 0, "wrong_level": 0, "employer": 0, "pay": 1, "location_or_mode": 0, "other": 1}


async def _counts_as_before(session, owner) -> dict:
    """The counts as they were taken: one statement grouped by status and ended reason (the reference)."""
    from app.jobsearch.models import JobPosting
    from app.jobsearch.pipeline import board
    from app.jobsearch.pipeline import from_posting as role_from_posting

    lanes = {key: 0 for key, _ in stages.LANES}
    closed = dict.fromkeys(board.CLOSED_LABELS, 0)
    starred = 0
    for status, reason, n, fav in (await session.execute(
            select(Opportunity.status, Opportunity.ended_reason, func.count(), func.count(Opportunity.favorited_at))
            .where(Opportunity.owner == owner).group_by(Opportunity.status, Opportunity.ended_reason))).all():
        lane = live.lane_of(status)
        if lane in lanes:
            lanes[lane] += n
        if status in stages.CLOSED:
            closed[board.closed_reason_key(status, reason)] += n
        starred += fav
    groups = {key: sum(lanes[s] for s in statuses) for key, statuses in stages.GROUPS}
    lanes = {board.ADDED_LINKS: await session.scalar(select(func.count()).select_from(JobPosting).where(
        *role_from_posting.waiting_added_where(owner))) or 0, **lanes}
    return {"lanes": lanes, "groups": groups, "closed": closed, "closed_total": sum(closed.values()),
            "favorites": starred}


async def test_the_counts_are_the_same_as_the_one_wide_grouping_gave_and_read_the_roles_narrowly(js):
    now = datetime.now(UTC)
    await _add(
        _role("g1", stages.GATED_OUT, favorited_at=now), _role("g2", stages.GATED_OUT),
        _role("no", stages.REJECTED, ended_reason="they_said_no", favorited_at=now),
        _role("gone", stages.WITHDREW, ended_reason="posting_gone"),
        _role("odd", stages.WITHDREW, ended_reason="unknown"), _role("bare", stages.GONE_QUIET),
        _role("a"), _role("b", favorited_at=now), _role("c", stages.READY_TO_LAUNCH, favorited_at=now),
        _role("d", stages.READY_TO_LAUNCH))
    statements: list[str] = []
    noted = lambda _conn, _cursor, statement, *_rest: statements.append(statement)  # noqa: E731
    engine = db.engine().sync_engine
    async with db.session_factory()() as session:
        event.listen(engine, "before_cursor_execute", noted)
        try:
            new = await live.counts(session, OWNER)
        finally:
            event.remove(engine, "before_cursor_execute", noted)
        assert new == await _counts_as_before(session, OWNER)
    assert new["favorites"] == 4 and new["closed_total"] == 6
    opps = [s for s in statements if "js_opportunities" in s]
    by_reason = [s for s in opps if "GROUP BY" in s and "ended_reason" in s]
    assert by_reason and all(" IN (" in s.split("GROUP BY")[0] for s in by_reason), by_reason  # ended roles only
    assert len([s for s in statements if s.startswith("SELECT")]) <= 4, statements


async def test_the_stream_says_hello_with_the_counts_then_each_change_and_a_ping_when_quiet(js):
    await _add(_role("d"), _role("e", stages.READY_TO_LAUNCH))
    gone = asyncio.Event()

    async def disconnected() -> bool:
        return gone.is_set()

    stream = live.stream(OWNER, disconnected, heartbeat=0.2)
    hello = await anext(stream)
    assert hello.startswith("id: 0\nevent: hello\n")
    counts = json.loads(hello.split("data: ", 1)[1])["counts"]
    assert counts["lanes"][stages.READY_TO_LAUNCH] == 1 and counts["lanes"][stages.FIRST_IMPRESSION] == 1
    assert "added_links" in counts["lanes"]
    await _add(_role("f"))
    # The counts go out a moment after a commit, several commits as one message, so a counts message for the roles
    # added before the stream opened can still come first (it did under the full suite's load). The frames are read in
    # the broker's order until the role's own, a counts message after it and a ping have all come.
    frames: list[str] = []

    def done() -> bool:
        at = next((i for i, f in enumerate(frames) if '"title":"Role f"' in f), None)
        return (at is not None and ": ping\n\n" in frames
                and any("\nevent: counts\n" in f for f in frames[at + 1:]))

    while not done():
        assert len(frames) < 12, f"the role, the counts after it and a ping never all came: {frames}"
        frames.append(await anext(stream))
    assert "\nevent: role\n" in next(f for f in frames if '"title":"Role f"' in f)
    gone.set()
    await stream.aclose()
    assert not live.broker.listening(OWNER), "a page that went away is no longer sent anything"


async def test_a_task_raised_answered_and_put_off_is_announced_as_the_inbox_shows_it(listening):
    from app.inbox import service as inbox

    async with db.session_factory()() as session:
        task = await inbox.raise_task(session, OWNER, key="k-merge", title="Merge A into B?", kind="decision",
                                      options=[{"id": "merge", "label": "Merge them"}, {"id": "keep", "label": "Keep"}],
                                      recommended="merge")
        await session.commit()
        task_id = task.id
    (kind, raised), = await _drain(listening)
    assert kind == "task" and raised["id"] == str(task_id) and raised["state"] == "open"
    assert raised["title"] == "Merge A into B?" and raised["recommended"] == "merge" and raised["created_at"]
    async with db.session_factory()() as session:
        await inbox.snooze(session, OWNER, task_id, date(2030, 1, 2))
        await session.commit()
    (kind, put_off), = await _drain(listening)
    assert kind == "task" and put_off["snoozed_until"] == "2030-01-02"
    async with db.session_factory()() as session:
        await inbox.answer(session, OWNER, task_id, "keep")
        await session.commit()
    tasks = [d for k, d in await _drain(listening) if k == "task"]
    assert [(t["state"], t["answer"]) for t in tasks] == [("done", "keep")]
    async with db.session_factory()() as session:
        await session.delete(await inbox.get_task(session, OWNER, task_id))
        await session.commit()
    assert await _drain(listening) == [("task", {"id": str(task_id), "deleted": True})]


async def test_a_notification_raised_and_read_is_announced_with_whether_it_was_read_before(listening):
    from app.inbox import service as inbox

    async with db.session_factory()() as session:
        note = await inbox.notify(session, OWNER, key="k-note", kind="note", title="Something to know")
        await session.commit()
        note_id = note.id
    (kind, raised), = await _drain(listening)
    assert kind == "notification" and raised["id"] == str(note_id) and raised["read_at"] is None
    assert raised["was_read"] is None
    async with db.session_factory()() as session:
        await inbox.mark_read(session, OWNER, None)
        await session.commit()
    (kind, read), = await _drain(listening)
    assert kind == "notification" and read["read_at"] and read["was_read"] is False
    async with db.session_factory()() as session:
        assert await inbox.mark_read(session, OWNER, note_id) == 0
        await session.commit()
    assert await _drain(listening) == [], "reading a read one again says nothing"


async def test_a_stream_gets_only_the_topics_it_asked_for(js):
    from app.inbox import service as inbox

    sub = live.broker.subscribe(OWNER, frozenset({"task", "notification"}))
    try:
        await _add(_role("t"))
        async with db.session_factory()() as session:
            await inbox.notify(session, OWNER, key="k-t", kind="note", title="Told")
            await session.commit()
        await asyncio.sleep(live.COUNTS_AFTER_SECONDS + 0.3)
        assert [k for k, _ in await _drain(sub, counts=True)] == ["notification"], (
            "no role and no counts for a page that shows neither"
        )
        live.broker.publish(OWNER, "resync", {})
        assert [k for k, _ in await _drain(sub, counts=True)] == ["resync"], "resync goes to every stream"
    finally:
        live.broker.unsubscribe(sub)
    assert live.parse_topics(None) is None and live.parse_topics("task, notification") == {"task", "notification"}
    with pytest.raises(ValueError):
        live.parse_topics("task,nonsense")


async def test_a_stream_without_roles_says_hello_without_the_counts(js):
    async def disconnected() -> bool:
        return False

    stream = live.stream(OWNER, disconnected, heartbeat=0.2, topics=frozenset({"task"}))
    hello = await anext(stream)
    assert json.loads(hello.split("data: ", 1)[1]) == {}
    await stream.aclose()


async def test_the_live_route_refuses_a_topic_there_is_none_of(client):
    assert (await client.get("/api/v1/live?topics=nonsense")).status_code == 422


async def test_a_move_answers_with_the_roles_card_and_the_counts(client):
    (role_id,) = await _add(_role("g"))
    moved = (await client.post(f"/api/v1/roles/{role_id}/move", json={"to": stages.APPLIED})).json()
    assert moved["id"] == str(role_id) and moved["status"] == stages.APPLIED and moved["favorite"] is False
    assert moved["counts"]["lanes"][stages.APPLIED] == 1 and moved["counts"]["lanes"][stages.FIRST_IMPRESSION] == 0
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        role.status = stages.REJECTED
        await session.commit()
    back = (await client.post(f"/api/v1/roles/{role_id}/move-back")).json()
    assert back["status"] == stages.FIRST_IMPRESSION, "Applied is no place a role goes back to: First Impression"
    assert back["counts"]["closed_total"] == 0
