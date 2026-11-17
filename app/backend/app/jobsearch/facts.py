"""The engine's facts (`js_engine_facts`): short texts the engine writes and the page reads, so that nothing the owner
sees lives only in the engine's memory (the split of the app into a page and an engine, the owner's point 3). One row
for an owner and a key; a write replaces the text and stamps the time.

Every function takes the caller's session and only flushes: the caller commits, as every service does. A write takes
the writer's turn like any write, and a read works in a reader's session too. Reading then writing in one transaction
is safe between the two programs: every writer's transaction begins `BEGIN IMMEDIATE` (`db._guard`), so no other write
gets in between the read and the write.

The statements are the ORM's (`add`, `delete`), never a Core `insert`, `update` or `delete`: the work queue's scan
(`tests/jobsearch/work_queue/scan.py`) would list each of those as a direct write.

The keys, and who writes them (the later lanes of the split fill them in):

- `night_left`, `night_spend`: the "left undone" and the spend lines the Agents page shows, written when the off-hours
  window ends;
- `browser_down`: the note that the browser is down;
- `board_feed:next_search`: where in the owner's searches the next night's board read begins;
- `asked:<kind>`, `owner_asked:<kind>`: the "Run now" asks of the pool and of the owner, by session kind;
- `last_pass:housekeeping`, `last_pass:dispatcher`: when the engine's last passes finished, for the owner door's
  `health`;
- `ask:<id>`, `answer:<id>`: a request the page makes of the engine, and the engine's answer to it;
- `said:<kind>:<version>`: what the engine said when it finished a piece of work the owner is waiting on.

Facts whose key starts `ask:`, `answer:` or `said:` are forgotten a day after they were written (`age_out`, which the
hour tick runs); the others stay until their writer replaces or drops them."""

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.models import EngineFact
from app.timeutil import utc_now

#: The keys `age_out` forgets, by how they start, and how old a fact must be to go.
FORGOTTEN_PREFIXES = ("ask:", "answer:", "said:")
FORGOTTEN_AFTER = timedelta(days=1)


async def _row(session: AsyncSession, owner: str, key: str) -> EngineFact | None:
    """The fact's row as the database has it now (a copy the session kept from an earlier transaction is refreshed)."""
    found = await session.scalars(
        select(EngineFact).where(EngineFact.owner == owner, EngineFact.key == key)
        .execution_options(populate_existing=True))
    return found.one_or_none()


async def put(session: AsyncSession, owner: str, key: str, value: str, *, now: datetime | None = None) -> None:
    """Set the fact `key` of `owner` to `value` in the caller's transaction: a new row, or the old row changed. The time
    is stamped either way (`now` is for a test's clock)."""
    moment = now or utc_now()
    row = await _row(session, owner, key)
    if row is None:
        session.add(EngineFact(owner=owner, key=key, value=value, updated_at=moment))
    else:
        row.value = value
        row.updated_at = moment
    await session.flush()


async def get(session: AsyncSession, owner: str, key: str) -> str | None:
    """The text of the fact, or None when there is none."""
    found = await session.execute(select(EngineFact.value).where(EngineFact.owner == owner, EngineFact.key == key))
    return found.scalar_one_or_none()


async def get_many(session: AsyncSession, owner: str, prefix: str) -> dict[str, str]:
    """Every fact of `owner` whose key starts with `prefix`, by key. The prefix is text, not a pattern."""
    found = await session.execute(
        select(EngineFact.key, EngineFact.value)
        .where(EngineFact.owner == owner, EngineFact.key.startswith(prefix, autoescape=True))
        .order_by(EngineFact.key))
    return {key: value for key, value in found.all()}


async def drop(session: AsyncSession, owner: str, key: str) -> bool:
    """Remove the fact in the caller's transaction. True when there was one."""
    row = await _row(session, owner, key)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def age_out(session: AsyncSession, owner: str, now: datetime | None = None) -> int:
    """Remove `owner`'s facts that start with one of `FORGOTTEN_PREFIXES` and were written more than a day before `now`.
    Returns how many went. No other key is touched."""
    cutoff = (now or utc_now()) - FORGOTTEN_AFTER
    named = or_(*(EngineFact.key.startswith(prefix, autoescape=True) for prefix in FORGOTTEN_PREFIXES))
    old = (await session.scalars(
        select(EngineFact).where(EngineFact.owner == owner, named, EngineFact.updated_at < cutoff))).all()
    for row in old:
        await session.delete(row)
    await session.flush()
    return len(old)
