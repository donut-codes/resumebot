"""The check of a copy: the new file against the old database, as of the same snapshot.

- the rows of every table counted on both sides;
- the tables the copy leaves the noise copies out of (`noise.py`): the rows of the old database less the ones left out
  (counted from the old database, not from the copy's own count), the ids of the copy equal to the old ids less the
  left-out ones, every raw copy a kept cleansed copy names (`from_raw`) in the copy, and the rule run again on the copy
  finding nothing more to leave out but the newest rows the copy kept to keep a posting reading the same;
- `PRAGMA foreign_key_check` (the load ran with foreign keys off, so this is where a stray row shows) and
  `PRAGMA integrity_check`;
- a sample of rows of every table (the first and the last by key, and others chosen at random) read from both and
  compared column by column, each value as the app would read it: uuids and times as objects, a date as a date, JSON
  parsed but with a JSON `null` kept apart from a missing value, a blob byte for byte. A day the schema adds is compared
  with the day worked out from the old row's time. The stored order is left out (`plan.RECOMPUTED`): the app works it
  out again.

A finding names the table, the row's key and the columns, never a value: the data is the owner's.
"""

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.migrate import convert, noise, plan
from app.migrate.plan import TablePlan
from app.timeutil import aware

#: How many rows of each table are compared (the first, the last and the rest at random).
SAMPLE = 25
#: How many rows of one table are named when they differ.
NAMED = 5


@dataclass
class Checked:
    """One table: its rows in the old database and in the copy, and how many were compared."""

    table: str
    source: int
    target: int
    compared: int
    #: Rows of the old database the copy left out (the noise copies and their cleansed copies).
    dropped: int = 0


@dataclass
class Verified:
    """What the check found: a line for each table, and the problems, in words (none when the copy checks out)."""

    tables: list[Checked] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _json_same(old: Any, new: Any) -> bool:
    if (old is None) != (new is None):
        return False  # a JSON null and a missing value are two things
    return old is None or json.loads(old) == json.loads(new)


def same(sort: str, old: Any, new: Any) -> bool:
    """Whether a value of the old database and the one the copy holds are the same value."""
    if sort == convert.JSON:
        return _json_same(old, new)
    if old is None or new is None:
        return old is None and new is None
    if sort == convert.UUID:
        return uuid.UUID(str(old)) == uuid.UUID(str(new))
    if sort == convert.DATETIME:
        return aware(old) == aware(new)
    if sort == convert.BLOB:
        return bytes(old) == bytes(new)
    if sort == convert.BOOL:
        return bool(old) == bool(new)
    return old == new


def _read(column: sa.Column[Any]) -> Any:
    """The column as the check reads it from the copy: a JSON column as the text stored (the type would read both
    `null` and nothing as `None`), any other as the app reads it."""
    return sa.type_coerce(column, sa.Text) if convert.kind(column) == convert.JSON else column


async def _left_out(source: Any, one: TablePlan, left: noise.Noise) -> int:
    """How many rows of this table of the old database the copy left out, counted in the old database."""
    if one.name == noise.RAW:
        return len(left.dropped)
    if one.name == noise.CLEANSED:
        return int(await source.fetchval(
            f'SELECT count(*) FROM "{noise.CLEANSED}" WHERE raw_id = ANY($1::uuid[])', list(left.dropped)))
    return 0


async def _table(source: Any, connection: sa.Connection, one: TablePlan, sample: int, problems: list[str],
                 left: noise.Noise) -> Checked:
    table, key = one.table, one.key
    old = int(await source.fetchval(f'SELECT count(*) FROM "{one.name}"'))
    gone = await _left_out(source, one, left)
    new = int(connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0)
    if old - gone != new:
        problems.append(f"{one.name}: {old} rows in the old database" + (f" less {gone} left out" if gone else "")
                        + f", {new} in the copy")
    keys = list(connection.scalars(sa.select(key).order_by(sa.func.random()).limit(sample)))
    keys += [connection.scalar(sa.select(key).order_by(key.asc()).limit(1)),
             connection.scalar(sa.select(key).order_by(key.desc()).limit(1))]
    keys = [k for k in dict.fromkeys(keys) if k is not None]
    if not keys:
        return Checked(one.name, old, new, 0, gone)
    recomputed = plan.RECOMPUTED.get(one.name, set())
    columns = [c for c in one.read if c.name not in recomputed]
    days = one.days
    names = ", ".join(f'"{c.name}"' for c in one.read)
    found = {str(r[key.name]): left.expected(one.name, dict(r)) for r in await source.fetch(
        f'SELECT {names} FROM "{one.name}" WHERE "{key.name}" = ANY($1)', keys)}
    copied = connection.execute(sa.select(*(_read(c) for c in columns), *days).where(key.in_(keys))).all()
    wide = len(columns)
    differ: list[str] = []
    for row in copied:
        was = found.get(str(row[[c.name for c in columns].index(key.name)]))
        mine = {c.name: v for c, v in zip(columns, row[:wide], strict=True)}
        if was is None:
            differ.append(f"{one.name}: a row of the copy is not in the old database")
            continue
        bad = [c.name for c, v in zip(columns, row[:wide], strict=True) if not same(convert.kind(c), was[c.name], v)]
        bad += [c.name for c, v in zip(days, row[wide:], strict=True)
                if v != convert.day_of(was[days[c]])]
        if bad:
            differ.append(f"{one.name}: the row with {key.name} {mine[key.name]} differs in {', '.join(bad)}")
    problems += differ[:NAMED]
    if len(differ) > NAMED:
        problems.append(f"{one.name}: {len(differ) - NAMED} more rows differ")
    return Checked(one.name, old, new, len(copied), gone)


def _ids(connection: sa.Connection, table: str) -> set[uuid.UUID]:
    return {noise.uid(r[0]) for r in connection.exec_driver_sql(f'SELECT id FROM "{table}"')}


async def _noise(source: Any, connection: sa.Connection, left: noise.Noise, problems: list[str]) -> None:
    """The copy's raw and cleansed copies are the old ones less the ones left out, nothing a kept copy names is missing,
    and the rule, run again on the copy, finds no copy to leave out that the copy does not hold for a reason."""
    old_raw = {noise.uid(r["id"]) for r in await source.fetch(f'SELECT id FROM "{noise.RAW}"')}
    cleansed = {noise.uid(r["id"]): noise.uid(r["raw_id"])
                for r in await source.fetch(f'SELECT id, raw_id FROM "{noise.CLEANSED}"')}
    new_raw, new_cleansed = _ids(connection, noise.RAW), _ids(connection, noise.CLEANSED)
    want_raw = old_raw - set(left.dropped)
    want_cleansed = {c for c, r in cleansed.items() if r not in left.dropped}
    for name, want, have in ((noise.RAW, want_raw, new_raw), (noise.CLEANSED, want_cleansed, new_cleansed)):
        if want != have:
            problems.append(f"{name}: the copy should hold the old rows less the left-out ones; {len(want - have)} "
                            f"are missing and {len(have - want)} are extra")
    named = set()
    for (text,) in connection.exec_driver_sql(f'SELECT from_raw FROM "{noise.CLEANSED}"'):
        named.update(noise.uid(n) for n in json.loads(text or "[]"))
    if named - new_raw:
        problems.append(f"{noise.CLEANSED}: {len(named - new_raw)} raw copies named by from_raw are not in the copy")
    again = noise.Decider(pinned=left.pinned)
    for rid, owner, source_id, ext_id, kind, parsed in connection.exec_driver_sql(
            f'SELECT id, owner, source_id, ext_id, kind, parsed FROM "{noise.RAW}" '
            "ORDER BY owner, source_id, ext_id, seq"):
        again.feed((owner, source_id, ext_id), rid, kind, parsed)
    again.finish()
    more = set(again.dropped) - left.held
    if more:
        problems.append(f"{noise.RAW}: the rule would still leave out {len(more)} raw copies of the copy")


async def verify(source: Any, target: Path, plans: list[TablePlan], left: noise.Noise | None = None,
                 sample: int = SAMPLE) -> Verified:
    """Check the file at `target` against `source` (an asyncpg connection in the copy's snapshot), table by table.
    `left` is what the copy left out (`noise.find`)."""
    left = left or noise.Noise()
    out = Verified()
    engine = sa.create_engine(f"sqlite:///{target.as_posix()}")
    try:
        with engine.connect() as connection:
            integrity = [str(r[0]) for r in connection.exec_driver_sql("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                out.problems.append("integrity_check: " + "; ".join(integrity[:5]))
            strays = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if strays:
                where = sorted({str(r[0]) for r in strays})
                out.problems.append(f"foreign_key_check: {len(strays)} rows point at nothing, in {', '.join(where)}")
            for one in plans:
                out.tables.append(await _table(source, connection, one, sample, out.problems, left))
            await _noise(source, connection, left, out.problems)
    finally:
        engine.dispose()
    return out
