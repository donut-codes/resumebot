"""A named lock is no SQL statement (`db.transaction_lock`), so a test that reads the order of a transaction's work from
the `transactions` fixture's timeline would not see it. This fixture puts a line in that timeline each time a lock is
taken: `NOTE` and the lock's name. A test module imports it (`from tests.lock_notes import ...  # noqa: F401`) and the
notes are there for each test that asks for `transactions`."""

import pytest

from app import db

NOTE = "named lock "


@pytest.fixture(autouse=True)
def named_locks_in_the_timeline(request, monkeypatch):
    if "transactions" not in request.fixturenames:
        return
    seen = request.getfixturevalue("transactions")
    real = db.transaction_lock

    async def noted(session, key, **kwargs):
        got = await real(session, key, **kwargs)
        seen.timeline.append(f"{NOTE}{key}")
        return got

    monkeypatch.setattr(db, "transaction_lock", noted)
