"""The mail read begins its two background tasks through the engine door by name (`mail_read`, `mail_close`), not with a
`create_task` of its own (`mail/sync.py`). Where the local door is installed the task runs as it always did; a door that
only records starts runs nothing."""

import asyncio
import uuid
from datetime import date

from app.jobsearch.mail import sync as mail_sync
from tests.support import OWNER

SINCE = date(2026, 11, 4)


async def test_the_local_door_runs_the_read_once_with_what_it_was_asked(monkeypatch):
    seen: list = []

    async def fake_finish(entry_id, owner, since, resume=False):
        seen.append((entry_id, owner, since, resume))

    monkeypatch.setattr(mail_sync, "_finish", fake_finish)
    entry_id = uuid.uuid4()
    mail_sync.start_in_background(entry_id, OWNER, SINCE, resume=True)
    await asyncio.gather(*list(mail_sync._tasks))
    assert seen == [(entry_id, OWNER, SINCE, True)]


async def test_the_local_door_runs_the_close_once_with_what_it_was_asked(monkeypatch):
    seen: list = []

    async def fake_close(entry_id, owner):
        seen.append((entry_id, owner))

    monkeypatch.setattr(mail_sync, "_close", fake_close)
    entry_id = uuid.uuid4()
    mail_sync.start_close_in_background(entry_id, OWNER)
    await asyncio.gather(*list(mail_sync._tasks))
    assert seen == [(entry_id, OWNER)]


async def test_a_recording_door_gets_one_mail_read_start_and_runs_nothing(monkeypatch, fake_starts):
    seen: list = []

    async def fake_finish(*args, **kwargs):
        seen.append(args)

    monkeypatch.setattr(mail_sync, "_finish", fake_finish)
    entry_id = uuid.uuid4()
    tasks_before = set(mail_sync._tasks)
    mail_sync.start_in_background(entry_id, OWNER, SINCE)
    await asyncio.sleep(0)
    assert fake_starts.starts == [("mail_read", (entry_id, OWNER, SINCE, False))]
    assert seen == [] and set(mail_sync._tasks) == tasks_before


async def test_a_recording_door_gets_one_mail_close_start_and_runs_nothing(monkeypatch, fake_starts):
    seen: list = []

    async def fake_close(*args):
        seen.append(args)

    monkeypatch.setattr(mail_sync, "_close", fake_close)
    entry_id = uuid.uuid4()
    tasks_before = set(mail_sync._tasks)
    mail_sync.start_close_in_background(entry_id, OWNER)
    await asyncio.sleep(0)
    assert fake_starts.starts == [("mail_close", (entry_id, OWNER))]
    assert seen == [] and set(mail_sync._tasks) == tasks_before
