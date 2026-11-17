"""The retry of a failed wake is started by name: `wake_ops.send` asks the engine door, and the local door keeps the
one pending retry (plan lane `engine-calls-starts-b`). No network, no database, no sleeping."""

import asyncio
from datetime import UTC, datetime

from app.jobsearch.work import engine_calls
from app.settings import settings
from app.sites import wake_ops
from app.sites.agent_calls import Wake


async def test_a_failed_wake_asks_the_engine_door_by_name_for_its_retry(fake_starts, monkeypatch):
    async def refused(project_id):
        return False

    monkeypatch.setattr(wake_ops, "_knock", refused)
    monkeypatch.setattr(wake_ops, "RETRY_DELAYS", ())  # the old code would start a real retry: it ends at once
    monkeypatch.setattr(wake_ops, "_retrying", {})
    monkeypatch.setattr(settings, "wake_url", "http://scout.test/wake")
    assert await wake_ops.send("project-1", Wake("plan_approved", "do")) is False
    assert len(fake_starts.starts) == 1
    name, key = fake_starts.starts[0]
    assert name == "wake_retry" and key[0] == "project-1" and isinstance(key[1], datetime)
    assert wake_ops._retrying == {}, "with the door recording, no retry is kept here"


async def test_the_local_door_keeps_the_retry_it_was_asked_for(monkeypatch):
    ran, release = [], asyncio.Event()

    async def retry(project_id, since):
        ran.append(project_id)
        await release.wait()

    monkeypatch.setattr(wake_ops, "_retry", retry)
    monkeypatch.setattr(wake_ops, "_retrying", {})
    engine_calls.start("wake_retry", ("project-1", datetime.now(UTC)))
    task = wake_ops._retrying["project-1"]
    release.set()
    await task
    assert ran == ["project-1"]
