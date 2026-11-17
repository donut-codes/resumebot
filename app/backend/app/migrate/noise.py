"""The noise copies a copy from Postgres leaves out: they add no value, so they are not copied.

An older version of the app stored a new raw copy at every read of a posting (`js_raw_postings`, with its cleansed copy
in `js_cleansed_postings`) even when only the bytes of the page changed and nothing a reader sees did. The `unchanged`
rule (`raw_copies.unchanged`) stops that. A **noise copy** is a raw copy that rule would not have stored: a list item of
a posting whose previous *kept* rows give the cleaner the same inputs once the item is folded in, by the rule's own
comparison (it is called here, not written a second time). The copier does not write a noise copy or its cleansed copies
into the new file.

**Always kept:** each posting's first item; every item that differs from the kept rows before it; every detail and
legacy row (the rule is about items); any raw copy a cleansed copy that another row points at belongs to (every foreign
key into either table, read from the old database's own constraints, so one added later is not missed: a rank names a
cleansed copy); and a posting's newest raw copy when leaving it out would change what the posting's current copy
reads (the newest kept row has no cleansed copy of the same highest cleaner version with the same fields): the current
copy and the posted day it gives read the same as before.

**What is rewritten:** a kept cleansed copy's `from_raw` names the raw rows it was built from; a name that is a noise
copy becomes the kept item it stood for (the same inputs by the rule), so nothing names a row that is not there.

The old database is only read. The decisions are made by a pure `Decider` fed the rows of a posting in the order they
were written, so the same code applies the rule to the old database (`find`), to the new file (the check) and to any
copy of the data.
"""

import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.jobsearch.postings import raw_copies
from app.jobsearch.postings.raw_copies import ITEM, Held
from app.migrate.plan import CopyError

RAW, CLEANSED = "js_raw_postings", "js_cleansed_postings"
#: The columns of a cleansed copy that say what a reader sees: two copies that agree on all of them read the same.
CONTENT = ("title", "employer", "locations", "work_mode", "pay_min", "pay_max", "posted_date", "text_clean", "removed",
           "marks", "partial", "no_raw")
JSON_COLUMNS = ("locations", "removed", "marks")
#: How many raw ids are asked about in one statement.
AT_ONCE = 500


def uid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def parse(text: Any) -> dict:
    """The `parsed` of a raw row as a dict (the source database's JSON as text, or as the SQLite file holds it)."""
    if isinstance(text, dict):
        return text
    value = json.loads(text) if text else {}
    return value if isinstance(value, dict) else {}


@dataclass
class Decider:
    """The rule, applied to rows fed one posting after another (each posting's rows in the order they were written).
    `dropped` maps a noise copy's id to the kept item it stood for; `tails` lists, for each posting whose newest row is
    dropped, that row and the newest kept row, which would become its newest."""

    pinned: frozenset[uuid.UUID] = frozenset()
    dropped: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    tails: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    _key: Any = None
    _kept: list[Held] = field(default_factory=list)
    _last: uuid.UUID | None = None

    def feed(self, key: Any, raw_id: Any, kind: str, parsed: Any) -> None:
        """The next row: `key` names its posting (any value; rows of one posting come together)."""
        if key != self._key:
            self.close()
            self._key = key
        rid = uid(raw_id)
        self._last = rid
        held = Held(rid, kind, "", parse(parsed))
        if kind == ITEM and rid not in self.pinned and self._kept and raw_copies.unchanged(
                self._kept, held.parsed, {self._kept[-1].id}):
            self.dropped[rid] = next(h.id for h in reversed(self._kept) if h.kind == ITEM)
            return
        self._kept.append(held)

    def close(self) -> None:
        """The posting fed so far is complete."""
        if self._last is not None and self._last in self.dropped:
            self.tails.append((self._last, self._kept[-1].id))
        self._kept, self._last = [], None

    def finish(self) -> None:
        self.close()
        self._key = None


@dataclass
class Noise:
    """What the copy leaves out: the dropped raw copies (each with the kept item it stood for), the raw copies kept
    though the rule would drop them (`held`), and how many cleansed copies went with the dropped (counted as the copy
    streams)."""

    dropped: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    held: set[uuid.UUID] = field(default_factory=set)
    pinned: frozenset[uuid.UUID] = frozenset()
    cleansed_dropped: int = 0

    def row(self, table: str, names: list[str]) -> Callable[[list[Any]], list[Any] | None] | None:
        """For a table the copy filters: the function from a row as read (a list of values) to the row to write, or None
        for a row to leave out. None for any other table."""
        if table == RAW:
            at = names.index("id")

            def raw(values: list[Any]) -> list[Any] | None:
                return None if uid(values[at]) in self.dropped else values

            return raw
        if table == CLEANSED:
            at_raw, at_from = names.index("raw_id"), names.index("from_raw")

            def cleansed(values: list[Any]) -> list[Any] | None:
                if uid(values[at_raw]) in self.dropped:
                    self.cleansed_dropped += 1
                    return None
                values[at_from] = self.stand_in(values[at_from])
                return values

            return cleansed
        return None

    def stand_in(self, from_raw: Any) -> Any:
        """A cleansed copy's `from_raw` with each dropped raw copy replaced by the kept item it stood for; text that
        names no dropped copy is returned as it came."""
        if from_raw is None:
            return None
        names = json.loads(from_raw) if isinstance(from_raw, str | bytes) else from_raw
        if not any(uid(n) in self.dropped for n in names):
            return from_raw
        return json.dumps([str(self.dropped.get(uid(n), uid(n))) for n in names])

    def expected(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """A row of the old database as the copy holds it (what the check compares the copy's row with)."""
        if table == CLEANSED:
            return {**row, "from_raw": self.stand_in(row["from_raw"])}
        return row


async def foreign_keys(source: Any) -> list[tuple[str, str, str]]:
    """Every foreign key of the old database into the raw or the cleansed copies, as (table, column, the table it
    names), leaving out the cleansed copies' own key to their raw copy (it goes with them). Each must name the other
    table's `id` and be of one column, or the copy stops: a pointer it cannot follow is one it could leave dangling."""
    rows = await source.fetch(
        "SELECT t.relname AS tbl, f.relname AS ref, array_length(c.conkey, 1) AS width, "
        "(SELECT string_agg(attname, ',') FROM pg_attribute WHERE attrelid = c.conrelid AND attnum = ANY(c.conkey)) "
        "AS cols, (SELECT string_agg(attname, ',') FROM pg_attribute WHERE attrelid = c.confrelid "
        "AND attnum = ANY(c.confkey)) AS refcols FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_class f ON f.oid = c.confrelid WHERE c.contype = 'f' AND f.relname = ANY($1::text[]) "
        "AND f.relnamespace = current_schema()::regnamespace", [RAW, CLEANSED])
    out = []
    for r in rows:
        if r["tbl"] == CLEANSED and r["cols"] == "raw_id" and r["ref"] == RAW:
            continue
        if r["width"] != 1 or r["refcols"] != "id":
            raise CopyError(f"{r['tbl']}.{r['cols']} points at {r['ref']} in a way the copy cannot follow")
        out.append((r["tbl"], r["cols"], r["ref"]))
    return sorted(out)


async def pinned(source: Any, keys: list[tuple[str, str, str]]) -> frozenset[uuid.UUID]:
    """The raw copies other rows point at: the ones named by a key into the raw copies, and the ones the cleansed
    copies named by a key into those belong to."""
    raw: set[uuid.UUID] = set()
    cleansed: set[uuid.UUID] = set()
    for table, column, ref in keys:
        found = await source.fetch(f'SELECT DISTINCT "{column}" AS id FROM "{table}" WHERE "{column}" IS NOT NULL')
        (raw if ref == RAW else cleansed).update(uid(r["id"]) for r in found)
    ids = sorted(cleansed)
    for at in range(0, len(ids), AT_ONCE):
        page = ids[at:at + AT_ONCE]
        for r in await source.fetch(f'SELECT raw_id FROM "{CLEANSED}" WHERE id = ANY($1::uuid[])', page):
            raw.add(uid(r["raw_id"]))
    return frozenset(raw)


async def decide(source: Any, held: frozenset[uuid.UUID]) -> Decider:
    """The rule applied to every posting of the old database: its raw rows read in the order they were written, a
    posting after another."""
    decider = Decider(pinned=held)
    async for r in source.cursor(
            f'SELECT id, owner, source_id, ext_id, kind, parsed FROM "{RAW}" ORDER BY owner, source_id, ext_id, seq',
            prefetch=200):
        decider.feed((r["owner"], r["source_id"], r["ext_id"]), r["id"], r["kind"], r["parsed"])
    decider.finish()
    return decider


def read(row: Any) -> tuple[Any, ...]:
    """A cleansed copy as compared: the fields a reader sees (JSON parsed, so key order and spacing do not count)."""
    return tuple(json.loads(row[c]) if c in JSON_COLUMNS and isinstance(row[c], str | bytes) else row[c]
                 for c in CONTENT)


def current(copies: Iterable[Any]) -> tuple[int, tuple[Any, ...]] | None:
    """Of one raw copy's cleansed copies, the highest cleaner version and what it reads (None for no copy)."""
    best = max(copies, key=lambda c: c["cleaner_version"], default=None)
    return None if best is None else (best["cleaner_version"], read(best))


async def protect(source: Any, tails: list[tuple[uuid.UUID, uuid.UUID]]) -> set[uuid.UUID]:
    """Of the postings whose newest raw row is dropped, the newest rows to keep after all: those whose posting would
    read differently without them (the newest kept row has no cleansed copy of the highest cleaner version, or one that
    reads another way). A tail is (the dropped newest row, the newest kept row)."""
    keep: set[uuid.UUID] = set()
    for at in range(0, len(tails), AT_ONCE):
        page = tails[at:at + AT_ONCE]
        ids = sorted({i for pair in page for i in pair})
        fetched = await source.fetch(
            f'SELECT raw_id, cleaner_version, {", ".join(CONTENT)} FROM "{CLEANSED}" WHERE raw_id = ANY($1::uuid[])',
            ids)
        by: dict[uuid.UUID, list[Any]] = {}
        for row in fetched:
            by.setdefault(uid(row["raw_id"]), []).append(row)
        for newest, kept in page:
            if current(by.get(newest, [])) != current(by.get(kept, [])):
                keep.add(newest)
    return keep


async def find(source: Any) -> Noise:
    """What the copy leaves out, worked out from the old database (read only)."""
    held = await pinned(source, await foreign_keys(source))
    decider = await decide(source, held)
    keep = await protect(source, decider.tails)
    found = Noise(dropped={k: v for k, v in decider.dropped.items() if k not in keep}, pinned=held)
    found.held = set(keep)
    return found
