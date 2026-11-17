"""The live sink (live_sink.py): a commit's items are read and published only for a page that is there, one delivery
after another. Synthetic names only; no database, since the builder is a stand-in."""

import asyncio
import uuid
from datetime import date
from typing import Any

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.pipeline import live, live_sink
from tests.support import OWNER


class Builds:
    """Stands in for `live_sink.from_rows`: notes each call and answers one message for each id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str], list[tuple[str, ...]]]] = []

    async def __call__(self, owner: str, kind: str, ids: list[str], tags: list[tuple[str, ...]]
                       ) -> list[dict[str, Any]]:
        self.calls.append((owner, kind, list(ids), list(tags)))
        if kind == "slow":
            await asyncio.sleep(0.05)
        if kind == "bad":
            raise RuntimeError("a made-up failure")
        return [{"id": row_id, "kind": kind} for row_id in ids]


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> live.Broker:
    """A broker of this test's own, so a stream here is not the next test's."""
    fresh = live.Broker()
    monkeypatch.setattr(live, "broker", fresh)
    return fresh


def drained(sub: live.Subscriber) -> list[tuple[str, dict]]:
    out = []
    while not sub.queue.empty():
        _, kind, data = sub.queue.get_nowait()
        out.append((kind, data))
    return out


async def test_no_page_listening_means_no_read(broker, monkeypatch):
    def boom():
        raise AssertionError("a reader was opened for a page that is not there")

    monkeypatch.setattr(db, "read_session_factory", boom)
    sink = live_sink.LocalSink()
    sink.announce("o", [("role", "1", ()), ("task", "2", ())])
    await sink.settle()


async def test_items_are_built_in_runs_of_one_kind_and_published_in_order(broker, monkeypatch):
    sub = broker.subscribe("o")
    monkeypatch.setattr(broker, "counts_soon", lambda owner: None)  # no counts task left for the next test
    builds = Builds()
    sink = live_sink.LocalSink(build=builds)
    sink.announce("o", [("task", "1", ()), ("task", "2", ("new",)), ("role", "3", ("was:applied",))])
    await sink.settle()
    assert builds.calls == [("o", "task", ["1", "2"], [(), ("new",)]),
                            ("o", "role", ["3"], [("was:applied",)])]
    assert drained(sub) == [("task", {"id": "1", "kind": "task"}), ("task", {"id": "2", "kind": "task"}),
                            ("role", {"id": "3", "kind": "role"})]


async def test_one_row_changed_twice_in_a_commit_is_two_messages(broker, monkeypatch):
    sub = broker.subscribe("o")
    monkeypatch.setattr(broker, "counts_soon", lambda owner: None)  # no counts task left for the next test
    builds = Builds()
    sink = live_sink.LocalSink(build=builds)
    sink.announce("o", [("role", "1", ("new",)), ("role", "1", ("changed:status", "was:applied"))])
    await sink.settle()
    assert [c[3] for c in builds.calls] == [[("new",)], [("changed:status", "was:applied")]]
    assert len(drained(sub)) == 2


async def test_the_counts_follow_a_role_and_only_a_role(broker, monkeypatch):
    broker.subscribe("o")
    asked: list[str] = []
    monkeypatch.setattr(broker, "counts_soon", asked.append)
    sink = live_sink.LocalSink(build=Builds())
    sink.announce("o", [("task", "1", ())])
    await sink.settle()
    assert asked == []
    sink.announce("o", [("task", "1", ()), ("role", "2", ())])
    await sink.settle()
    assert asked == ["o"]


async def test_a_kind_no_stream_asked_for_is_not_built_but_a_role_still_asks_for_the_counts(broker, monkeypatch):
    sub = broker.subscribe("o", frozenset({"task"}))
    asked: list[str] = []
    monkeypatch.setattr(broker, "counts_soon", asked.append)
    builds = Builds()
    sink = live_sink.LocalSink(build=builds)
    sink.announce("o", [("role", "1", ())])
    await sink.settle()
    assert builds.calls == [] and drained(sub) == []
    assert asked == ["o"]


async def test_deliveries_keep_the_order_the_commits_announced_them_in(broker):
    sub = broker.subscribe("o")
    sink = live_sink.LocalSink(build=Builds())
    sink.announce("o", [("slow", "1", ())])
    sink.announce("o", [("task", "2", ())])
    await sink.settle()
    assert [kind for kind, _ in drained(sub)] == ["slow", "task"]


async def test_a_build_that_fails_is_logged_and_the_next_delivery_still_goes(broker):
    sub = broker.subscribe("o")
    sink = live_sink.LocalSink(build=Builds())
    sink.announce("o", [("bad", "1", ())])
    sink.announce("o", [("task", "2", ())])
    await sink.settle()
    assert drained(sub) == [("task", {"id": "2", "kind": "task"})]


def test_a_commit_outside_any_loop_says_nothing_and_raises_nothing(broker):
    sub = broker.subscribe("o")
    live_sink.LocalSink(build=Builds()).announce("o", [("task", "1", ())])
    assert drained(sub) == []


def test_the_broker_says_whether_a_stream_asked_for_a_kind(broker):
    broker.subscribe("o", frozenset({"task"}))
    assert broker.wanted("o", "task") is True
    assert broker.wanted("o", "role") is False
    assert broker.wanted("o", "resync") is True
    assert broker.wanted("someone else", "task") is False


def test_the_local_sink_is_in_use_until_another_is_put(monkeypatch):
    monkeypatch.setattr(live_sink, "_sink", None)
    assert isinstance(live_sink.current(), live_sink.LocalSink)

    class Other:
        def announce(self, owner: str, items: list[live_sink.Item]) -> None:
            pass

    other = Other()
    live_sink.use(other)
    assert live_sink.current() is other
    live_sink.use(None)
    assert isinstance(live_sink.current(), live_sink.LocalSink)


class Recorder:
    """A sink that keeps what it is told, in place of the local one."""

    def __init__(self) -> None:
        self.got: list[tuple[str, list[tuple[str, str, tuple[str, ...]]]]] = []

    def announce(self, owner: str, items: list[tuple[str, str, tuple[str, ...]]]) -> None:
        self.got.append((owner, list(items)))


@pytest.fixture
def recorder(js, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(live_sink, "_sink", rec)
    return rec


def _role(slug: str) -> Opportunity:
    return Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}",
                       status=stages.FIRST_IMPRESSION, origin="scout", match_key=f"co {slug}|role {slug}",
                       found=date(2030, 1, 2))


async def test_a_commit_keeps_ids_and_tags_and_hands_them_to_the_sink(recorder):
    role = _role("a")
    async with db.session_factory()() as session:
        session.add(role)
        await session.commit()
        role_id = str(role.id)
    assert recorder.got == [(OWNER, [("role", role_id, ("new",))])]
    recorder.got.clear()
    async with db.session_factory()() as session:
        row = await session.get(Opportunity, uuid.UUID(role_id))
        row.status = stages.APPLIED
        await session.commit()
    assert recorder.got == [(OWNER, [("role", role_id, ("changed:status", f"was:{stages.FIRST_IMPRESSION}"))])]


async def test_a_rolled_back_change_and_a_change_nobody_shows_are_not_handed_over(recorder):
    role = _role("b")
    async with db.session_factory()() as session:
        session.add(role)
        await session.commit()
    recorder.got.clear()
    async with db.session_factory()() as session:
        row = await session.get(Opportunity, role.id)
        row.status = stages.APPLIED
        await session.flush()
        await session.rollback()
    async with db.session_factory()() as session:
        row = await session.get(Opportunity, role.id)
        row.notes = "a note moves nothing"
        await session.commit()
    assert recorder.got == []


async def test_a_notification_says_whether_it_was_read_and_a_removed_task_says_it_was_removed(recorder):
    from app.inbox import service as inbox

    async with db.session_factory()() as session:
        note = await inbox.notify(session, OWNER, key="k-note", kind="note", title="Something to know")
        task = await inbox.raise_task(session, OWNER, key="k-task", title="Merge A into B?", kind="decision",
                                      options=[{"id": "merge", "label": "Merge them"}], recommended="merge")
        await session.commit()
        note_id, task_id = str(note.id), str(task.id)
    (owner, items), = recorder.got
    assert owner == OWNER
    assert ("notification", note_id, ("new",)) in items and ("task", task_id, ()) in items
    recorder.got.clear()
    async with db.session_factory()() as session:
        await inbox.mark_read(session, OWNER, None)
        await session.commit()
    assert recorder.got == [(OWNER, [("notification", note_id, ("was_read:no",))])]
    recorder.got.clear()
    async with db.session_factory()() as session:
        await session.delete(await inbox.get_task(session, OWNER, uuid.UUID(task_id)))
        await session.commit()
    assert recorder.got == [(OWNER, [("task", task_id, ("deleted",))])]


async def test_a_commit_with_no_page_listening_opens_no_reader(js, monkeypatch):
    opened: list[int] = []
    real = db.read_session_factory

    def spy():
        opened.append(1)
        return real()

    monkeypatch.setattr(db, "read_session_factory", spy)
    async with db.session_factory()() as session:
        session.add(_role("c"))
        await session.commit()
    await live_sink.settle()
    assert opened == []
