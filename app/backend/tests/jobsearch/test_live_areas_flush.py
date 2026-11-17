"""A row of a table behind a screen's area is flushed: one `changed` message names the area and the row, by the page's
own commit (the local sink) and by the engine's (a datagram the page's hub turns into the same message). Everything is
made up."""

import pytest
from process_engine.models import ProcInstance, ProcToken
from sqlalchemy import inspect as sa_inspect

from app import db, notice
from app.jobsearch import facts, stages
from app.jobsearch.models import (
    GapRun,
    GoRun,
    Interview,
    InterviewPrep,
    Mission,
    Opportunity,
    ParseJob,
)
from app.jobsearch.pipeline import live, live_hub, live_read, live_sink
from app.models.agent import AgentConnection, AgentRunner
from app.settings import settings
from tests.support import NOON, OWNER

#: area name -> (a made-up new row, the areas its message names, the id the message carries).
CASES = {
    "jobs": (lambda: ParseJob(owner=OWNER, text="A made-up posting.", state="waiting"), ["jobs"],
             lambda row: str(row.id)),
    "gaps": (lambda: GapRun(owner=OWNER, role_id="builders", title="Builders", kind="role", state="waiting"),
             ["gaps"], lambda row: str(row.id)),
    "agents": (lambda: AgentRunner(owner=OWNER, name="session-1"), ["agents"], lambda row: str(row.id)),
    "missions": (lambda: Mission(owner=OWNER, key="made-up", name="Made up", state="on"), ["missions"],
                 lambda row: str(row.id)),
    "harness": (lambda: ProcInstance(process_key="made.up", file_hash="h"), ["harness"], lambda row: str(row.id)),
    "go": (lambda: GoRun(owner=OWNER, state={}), ["go"], lambda row: row.owner),
    "ready": (lambda: AgentConnection(owner=OWNER, slug="scout", name="Scout", roles=[]), ["agents", "ready"],
              lambda row: str(row.id)),
}


class Told:
    """Stands in for `notice.tell_page`: it keeps each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list]] = []

    def __call__(self, owner: str, items) -> bool:
        self.calls.append((owner, [tuple(item) for item in items]))
        return True


@pytest.fixture
def listening(js, monkeypatch):
    """A stream of the owner that asked for `changed` only; the harness tables have no owner, so the app's owner is this
    test's."""
    monkeypatch.setattr(settings, "owner", OWNER)
    sub = live.broker.subscribe(OWNER, frozenset({"changed"}))
    yield sub
    live.broker.unsubscribe(sub)


def _messages(sub: live.Subscriber) -> list[dict]:
    out = []
    while not sub.queue.empty():
        _, kind, data = sub.queue.get_nowait()
        assert kind == "changed"
        out.append(data)
    return out


def _by_area(messages: list[dict]) -> dict[str, set[str]]:
    """The ids told for each area, whatever the messages that carried them. Several rows of one commit may come as one
    message or as a few: a screen reads again either way."""
    out: dict[str, set[str]] = {}
    for message in messages:
        out.setdefault(message["area"], set()).update(message["ids"])
    return out


async def _commit(*rows) -> None:
    async with db.session_factory()() as session:
        session.add_all(rows)
        await session.commit()


def _role(slug: str) -> Opportunity:
    return Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}",
                       status=stages.FIRST_IMPRESSION, origin="scout", match_key=f"co {slug}|role {slug}",
                       found=NOON.date())


@pytest.mark.parametrize("name", CASES)
async def test_a_new_row_in_each_area_is_one_changed_message_by_the_pages_own_commit(name, listening):
    make, areas, ident = CASES[name]
    row = make()
    await _commit(row)
    await live_sink.settle()
    assert _messages(listening) == [{"area": area, "ids": [ident(row)]} for area in areas]


@pytest.mark.parametrize("name", CASES)
async def test_a_new_row_in_each_area_is_one_item_by_the_engines_commit_and_one_message_on_the_page(
        name, js, monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(live, "COUNTS_AFTER_SECONDS", 0)  # no wait between an arrival and its send
    told = Told()
    monkeypatch.setattr(notice, "tell_page", told)
    monkeypatch.setattr(live_sink, "_sink", live_sink.WireSink())  # as in the engine
    make, areas, ident = CASES[name]
    row = make()
    await _commit(row)
    assert len(told.calls) == 1, "one datagram for one commit"
    owner, items = told.calls[0]
    assert owner == OWNER
    assert items == [("changed", ident(row), [area]) for area in areas], "ids and the area as text, no data"
    sub = live.broker.subscribe(OWNER, frozenset({"changed"}))
    try:
        hub = live_hub.Hub(lambda kind, ids: None, live_sink.LocalSink())  # as in the page
        hub.arrive(owner, items)
        await hub.settle()
        assert _messages(sub) == [{"area": area, "ids": [ident(row)]} for area in areas]
    finally:
        live.broker.unsubscribe(sub)


async def test_an_interview_and_its_prep_name_the_interview(listening):
    role = _role("p")
    await _commit(role)
    interview = Interview(owner=OWNER, role_id=role.id, when_at=NOON)
    await _commit(interview)
    await _commit(InterviewPrep(owner=OWNER, interview_id=interview.id, version=1))
    await live_sink.settle()
    assert _by_area(_messages(listening)) == {"prep": {str(interview.id)}}


async def test_the_steps_of_a_process_run_name_the_run(listening):
    run = ProcInstance(process_key="made.up", file_hash="h")
    await _commit(run)
    await _commit(ProcToken(instance_id=run.id, element_id="start"))
    await live_sink.settle()
    assert _by_area(_messages(listening)) == {"harness": {str(run.id)}}


async def test_a_changed_runner_and_a_paused_connection_are_told(listening):
    runner = AgentRunner(owner=OWNER, name="session-2")
    connection = AgentConnection(owner=OWNER, slug="scout", name="Scout", roles=[])
    await _commit(runner, connection)
    await live_sink.settle()
    _messages(listening)  # what the making said
    async with db.session_factory()() as session:
        beat = await session.get(AgentRunner, runner.id)
        paused = await session.get(AgentConnection, connection.id)
        assert beat is not None and paused is not None
        beat.seen_at = NOON
        paused.paused = True
        await session.commit()
    await live_sink.settle()
    assert _by_area(_messages(listening)) == {"agents": {str(runner.id), str(connection.id)},
                                              "ready": {str(connection.id)}}


async def test_a_row_set_to_what_it_was_says_nothing(listening):
    runner = AgentRunner(owner=OWNER, name="session-3")
    await _commit(runner)
    await live_sink.settle()
    _messages(listening)
    async with db.session_factory()() as session:
        row = await session.get(AgentRunner, runner.id)
        assert row is not None
        same = row.state
        row.state = same
        await session.commit()
    await live_sink.settle()
    assert _messages(listening) == []


async def test_a_row_flushed_three_times_in_one_commit_is_one_id(listening):
    async with db.session_factory()() as session:
        job = ParseJob(owner=OWNER, text="A made-up posting.", state="waiting")
        session.add(job)
        await session.flush()
        job.state = "reading"
        await session.flush()
        job.state = "done"
        await session.flush()
        job_id = str(job.id)
        await session.commit()
    await live_sink.settle()
    assert _messages(listening) == [{"area": "jobs", "ids": [job_id]}]


async def test_a_removed_row_is_told(listening):
    job = ParseJob(owner=OWNER, text="A made-up posting.", state="waiting")
    await _commit(job)
    await live_sink.settle()
    _messages(listening)
    async with db.session_factory()() as session:
        row = await session.get(ParseJob, job.id)
        assert row is not None
        await session.delete(row)
        await session.commit()
    await live_sink.settle()
    assert _messages(listening) == [{"area": "jobs", "ids": [str(job.id)]}]


async def test_only_the_facts_a_screen_shows_are_told(listening):
    async with db.session_factory()() as session:
        for key in ("night_left", "browser_down", "asked:researcher", "owner_asked:researcher",
                    "last_pass:dispatcher", "ask:1", "answer:1", "said:profile:2"):
            await facts.put(session, OWNER, key, "made up")
        await session.commit()
    await live_sink.settle()
    assert _by_area(_messages(listening)) == {
        "agents": {"night_left", "browser_down", "asked:researcher", "owner_asked:researcher"}}


def test_every_watched_table_names_known_areas_an_owner_and_an_id_column():
    for model, watch in live.AREA_WATCH.items():
        columns = sa_inspect(model).columns.keys()
        assert watch.areas and set(watch.areas) <= set(live_read.AREAS), model
        assert ("owner" in columns) == watch.owned, f"{model.__name__}: owned must say whether the table has an owner"
        assert watch.id_attr.startswith("=") or watch.id_attr in columns, f"{model.__name__}: no column {watch.id_attr}"


def test_every_area_has_a_table_behind_it():
    behind = {area for watch in live.AREA_WATCH.values() for area in watch.areas}
    assert behind == set(live_read.AREAS)
