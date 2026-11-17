"""The janitor (the work queue spec C2, I2): the to-dos nothing takes are deleted once an hour when they
are older than an hour, and counted in the log; a topic the dispatcher takes to-dos of (`consume.consumed_topics`) is
left to it. Everything is made up."""

import logging
from datetime import UTC, datetime, timedelta

from app import db
from app.jobsearch.models import Todo
from app.jobsearch.work.queue import janitor
from tests.jobsearch.work_queue.support import OWNER, posted_now

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
#: Two topics the dispatcher takes nothing of: `rules.changed` (its catch-ups are not built) and a made-up one.
UNTAKEN, ALSO_UNTAKEN = "rules.changed", "test.untaken"


async def _post(*rows: tuple[str, str, timedelta]) -> None:
    """To-dos of (topic, key) posted so long before NOW."""
    async with db.session_factory()() as session:
        for topic, key, age in rows:
            session.add(Todo(owner=OWNER, topic=topic, key=key, posted_at=NOW - age, not_before=NOW - age))
        await session.commit()


async def test_the_janitor_deletes_the_to_dos_an_hour_old_and_keeps_the_newer_ones(js, caplog):
    caplog.set_level(logging.INFO)
    await _post((UNTAKEN, "a", timedelta(hours=3)), (UNTAKEN, "b", timedelta(hours=2)),
                (ALSO_UNTAKEN, "", timedelta(minutes=61)), (ALSO_UNTAKEN, "", timedelta(minutes=59)),
                ("role.details", "", timedelta(minutes=1)))
    gone = await janitor.prune(NOW)
    assert gone == {UNTAKEN: 2, ALSO_UNTAKEN: 1}
    assert sorted(await posted_now()) == [("role.details", ""), (ALSO_UNTAKEN, "")]
    line = [r.getMessage() for r in caplog.records if "work-queue to-dos" in r.getMessage()]
    assert line == ["work-queue to-dos posted in the last hour: rules.changed=2, test.untaken=1 (3 in all)"]


async def test_a_pass_that_finds_nothing_old_logs_nothing(js, caplog):
    caplog.set_level(logging.INFO)
    await _post(("deck.dirty", "", timedelta(minutes=5)))
    assert await janitor.prune(NOW) == {}
    assert not [r for r in caplog.records if "work-queue to-dos" in r.getMessage()]


async def test_the_janitor_leaves_the_topics_the_dispatcher_takes_to_it(js):
    taken = ("posting.sift", "role.settle", "tick.hour", "pool.owed", "browser.reconcile", "ready.parser")
    await _post(*((topic, "", timedelta(hours=5)) for topic in taken), (UNTAKEN, "", timedelta(hours=5)))
    assert await janitor.prune(NOW) == {UNTAKEN: 1}
    assert sorted(await posted_now()) == sorted((topic, "") for topic in taken)
