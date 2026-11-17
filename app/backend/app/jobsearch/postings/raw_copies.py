"""The raw and the cleansed copy of what a read brings, in shadow:
nothing reads them (the owner, 2026-10-21).

**Raw** (`RawPosting`, append only): for each posting a read fetched, exactly what came back, with its source, the time
and the hash. A list feed's item is kept as the system answered it (a JSON item re-written with sorted keys, so the same
item hashes the same; a card of HTML as it was), a detail answer as the bytes of the reply. A read that fetches the same
bytes again writes nothing, and neither does a list read of a posting whose item changed nothing the cleaner reads (an
applicant count, a link: `unchanged`; the owner, 2026-11-06); a posting whose content changed adds a row, so its history
is kept. `parsed` is the plain posting the reader made of it (title, place, pay, text ...). `recleanse` runs the
reader's parse again on the bytes (`reparse.py`) and starts from `parsed` only for a row that cannot be parsed again.
For an item row, `RawPosting.fetched_at` is when the content last changed, not when the posting was last seen
(`JobPosting.last_seen`).

**Cleansed** (`CleansedPosting`, rebuildable): the posting's newest item and newest detail answer folded in the order
they were read (a value a later read gives replaces an earlier one, as the index does), made into one clean shape by
`cleanse.cleanse`, one row for each newest raw row and cleaner version. `recleanse` makes the rows a cleaner version has
not made yet from the raw rows alone; it never fetches.

**Where they are written.** `postings.store` (every adapter's and every recipe's list: the night's board read, a
site's read and the board's search save their items a slice at a time, `prepare` then `keep_prepared`, with the
cleaner's answer made before any lock is taken; a caller that asks for no slices keeps them in its one transaction,
`keep_items`), and the detail reads
(`postings.fill_details`, `read_places`, `posting_text.take`) call `keep_items` and `hold`; what the owner's
browser reported (an item of a scan, a page it captured), a role the scout reported and a tracker row call
`hold` (readers `browser`, `browser:page`, `code:page`, `code:text`, a followed board's page reader, `scout`, `import`):
the report as it came, not bytes of ours.
A posting read before this existed holds only its text: `keep_legacy` enters that text as a raw copy marked `no_raw`.
Those readers and `legacy` are the rows `recleanse` cannot parse again (`reparse.NOT_REPARSED`). Services flush;
the night's step commits.

**The current copy.** Every copy made here points its posting at itself (`JobPosting.current_cleansed_id`, `_point`),
in the transaction that writes the copy: the copy of a posting's newest raw row at the highest cleaner version is the
newest one made for it, so the pointer is the answer to "which copy is current" and `cleansed.py` reads it instead of
searching every raw row. A read that makes no copy (the same bytes, an item that changed nothing the cleaner reads)
leaves the pointer where it is.

**The copies go with their posting.** A raw row names its posting by the posting's address (`source_id` is the
posting's `employer_id`, with its `ext_id`), and everything here reads by that address. When an employer merge or
split gives a posting another address, its raw rows are given it too (`follow`): rows left under the old one are no
longer the posting's, and its current copy is lost with them. The rows of a posting that a merge folds into another
are not given to that one when it holds rows of its own: a copy is made from one posting's rows. One that holds none
takes them, whole, and their copy with them. One that holds rows whose copy has less text than the folded posting's
is given that text as a row of its own (`fuller_text`, `keep_text`): it has the role now, and a page that is read no
more (a closed posting, a job system with no page) would never give it the text again."""

import hashlib
import json
import re
import uuid
import zlib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import LargeBinary, Table, and_, bindparam, exists, func, or_, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app import db
from app.jobsearch import ats, off_turn
from app.jobsearch.measures.pay_place import counted_places, says_no_place
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity, RawPosting
from app.jobsearch.postings import cleanse, cleansed
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.work import queue
from app.sites import registry

ITEM, DETAIL, LEGACY = "item", "detail", "legacy"


def board_page() -> str:
    """The reader of a raw copy that is what the app's Chrome read out of a job board's posting page, as JSON (the
    board's capture): the board's copy of a posting, never the employer's. The label is the board hook's (`READER` of
    `registry.board_reader("capture")`), so it is asked for when needed: a public install, which has no such board,
    has none, and the answer is an empty string (no raw copy has an empty reader). `BOARD_PAGE` reads the same."""
    capture = registry.board_reader("capture")
    return str(getattr(capture, "READER", "")) if capture is not None else ""


def __getattr__(name: str) -> str:
    if name == "BOARD_PAGE":
        return board_page()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: The fields of a reader's posting that the cleaner starts from.
PLAIN_KEYS = ("title", "location", "work_mode", "comp", "comp_min", "comp_max", "posted", "url", "apply_url", "text",
              "department")
#: The keys of a plain posting that `cleanse.cleanse` reads, and no other: a copy folded from rows that give the cleaner
#: the same value of each of these is the same copy. A test reads `cleanse()` and fails when this and it disagree.
CLEANER_KEYS = ("title", "company", "location", "work_mode", "comp", "comp_min", "comp_max", "posted", "text")
#: A day as a reader writes it: ISO, with or without a time after it.
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
#: How many postings `keep_legacy` takes in one round.
LEGACY_BATCH = 200
#: How many postings' raw rows `heads` asks for in one statement, and how many rows' parse `with_parse` reads in one
#: (a parse holds the posting's whole text, so a statement of a hundred is a few milliseconds of decoding).
HEADS_AT_ONCE = 500
PARSED_AT_ONCE = 100
#: How many postings `heal_embedded` cleans again and saves in one short transaction.
HEAL_BATCH = 25


@dataclass
class Held:
    """One raw row as the cleaner needs it: not its bytes. `loaded` is False for a row `heads` read, whose parse
    (`parsed`) `with_parse` has yet to read: the whole parse of a board's rows is megabytes, and the cleaner needs it
    for the rows of the postings whose copy is made again only."""

    id: uuid.UUID
    kind: str
    sha: str
    parsed: dict
    loaded: bool = True


def body_of(raw: Any) -> tuple[bytes, str]:
    """The bytes to keep of an item a reader gave and their content type: JSON with sorted keys, a text as it is."""
    if isinstance(raw, bytes):
        return raw, "application/octet-stream"
    if isinstance(raw, str):
        return raw.encode("utf-8"), "text/html"
    body = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return body.encode("utf-8"), "application/json"


def deflate(body: bytes) -> bytes:
    return zlib.compress(body, 6)


def inflate(body: bytes) -> bytes:
    """The bytes of a raw row, as they were fetched."""
    return zlib.decompress(body)


def snapshot(listed: dict, company: str | None) -> dict:
    """The plain posting a reader made (`parsed`): an adapter's posting fields, and the company it is filed under."""
    out = {k: v for k, v in dict(listed).items() if k in PLAIN_KEYS}
    out["company"] = company
    return out


def of_posting(posting: JobPosting, text: str | None) -> dict:
    """The same plain posting, from the index row (the detail path: what the posting holds after the answer was kept).
    `text` is what that answer gave, never read from the row (its text is deferred in some reads)."""
    return {"title": posting.title, "company": posting.company, "location": posting.location,
            "work_mode": posting.work_mode, "comp": posting.comp, "comp_min": posting.comp_min,
            "comp_max": posting.comp_max, "posted": posting.posted.isoformat() if posting.posted else None,
            "url": posting.url, "apply_url": posting.apply_url, "department": posting.department, "text": text}


def keeps_places(held: Any, listed: Any) -> bool:
    """Whether the places `held` (towns joined with ";") stand against a `listed` place that is only a count or a word
    that is no place ("2 Locations", "Multiple Locations"): they do when they name no fewer towns than the count (a
    count with no number is no more than the towns named). A real place in `listed`, or no towns held, never keeps."""
    if not isinstance(listed, str) or not isinstance(held, str) or not says_no_place(listed) or says_no_place(held):
        return False
    counted = counted_places(listed)
    return counted is None or len([p for p in held.split(";") if p.strip()]) >= counted


def _fold(parsed: dict, later: dict) -> None:
    """Put the keys of `later` over `parsed`. A later row that says only a count of places does not take the place of
    the towns an earlier row named, as long as they are at least as many as the count (`keeps_places`; the index keeps
    them the same way): the nightly list read gives "2 Locations" after the page named the towns."""
    for key, value in later.items():
        if value is None or (key == "text" and not value):
            continue
        if key == "location" and keeps_places(parsed.get("location"), value):
            continue
        parsed[key] = value


def _use(rows: list[Held]) -> list[Held]:
    """The rows a posting's copy is made from: the newest of each kind, oldest first."""
    last = {h.kind: i for i, h in enumerate(rows)}
    return [rows[i] for i in sorted(last.values())]


def _cleaned(use: list[Held]) -> cleanse.Cleansed:
    """The cleaner's answer for the rows of `use` folded oldest to newest (the costly part of a copy: pure, no
    session)."""
    parsed: dict = {}
    for h in use:
        _fold(parsed, h.parsed)
    return cleanse.cleanse(parsed)


def cleaner_inputs(use: list[Held]) -> dict:
    """What the cleaner is given for the rows of `use` (`_use`) folded oldest to newest: the value of each of
    `CLEANER_KEYS` (None for a key never set, as the fold leaves it and `cleanse` reads it)."""
    parsed: dict = {}
    for h in use:
        _fold(parsed, h.parsed)
    return {key: parsed.get(key) for key in CLEANER_KEYS}


def _exact(a: Any, b: Any) -> bool:
    """Equal and of the same type, all the way down: `100000 == 100000.0` and `1 == True` in Python, but the cleaner
    keeps an int and drops the others, so the pay of one is not the pay of the other."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_exact(v, b[k]) for k, v in a.items())
    if isinstance(a, list | tuple):
        return len(a) == len(b) and all(_exact(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def same_inputs(a: dict, b: dict) -> bool:
    """Whether two `cleaner_inputs` are the same, each key equal and of the same type (`_exact`)."""
    return all(_exact(a[key], b[key]) for key in CLEANER_KEYS)


def known_day(rows: list[Held], parsed: dict) -> dict:
    """The plain posting of a new item (`parsed`), its `posted` day held at the day the posting's rows already give when
    the item's is later (docs/PRD.md 28.9, 40.1). A posting is posted once: a later read that says a later day (a site
    that stamps the moment of the read, "Posted Today" counted from the day of the read, "30+ days ago" counted from
    today) moves the day the posting looks old by, and made a raw row and a copy for every read. The day is the one the
    rows give the cleaner (`cleaner_inputs`, so a detail row's true day counts); an earlier day, none, or a value
    that is not a day is left as it is. `parsed` itself comes back when nothing is held."""
    known, new = cleaner_inputs(_use(rows)).get("posted"), parsed.get("posted")
    if (isinstance(known, str) and _DAY.match(known) and isinstance(new, str) and _DAY.match(new)
            and new[:10] > known[:10]):
        return {**parsed, "posted": known}
    return parsed


def unchanged(rows: list[Held], parsed: dict, current: Collection[uuid.UUID]) -> bool:
    """Whether a new item of a posting (`parsed`: its plain posting) changes nothing the cleaner reads, so that no row
    and no copy is kept for it (docs/PRD.md 28.9). All of: the posting already holds an item row (the first is always
    kept: a rebuild needs the list's bytes once); the newest of its raw rows has a cleansed copy of this cleaner version
    (`current`: the ids of such rows, from `current_copies`); and the cleaner would be given exactly what it was given
    for the rows it has, the new item replacing the newest item among them (`_use`, then `_fold`: the fold, not the item
    against the item, since a new item moves after a detail row and overrides it). `rows` have their parse read
    (`with_parse`). The baseline is the stored parse of those rows, not the parse the posting's current copy was built
    from: after a cleaner-version rebuild (`recleanse`, which parses the old bytes again) the two can differ, and an
    item that parses back to exactly the stored parse is then not kept, though the copy holds the newer parse of the
    old bytes (it takes an adapter change, then such a later item). Pure: it makes and saves nothing."""
    if not rows or rows[-1].id not in current or not any(h.kind == ITEM for h in rows):
        return False
    use = _use(rows)
    if not all(h.loaded for h in use):
        return False
    after = _use([*rows, Held(uuid.UUID(int=0), ITEM, "", parsed)])
    return same_inputs(cleaner_inputs(use), cleaner_inputs(after))


async def current_copies(session: AsyncSession, owner: str, postings: Iterable[list[Held]]) -> set[uuid.UUID]:
    """Of these postings' raw rows (each list as `heads` gave it), the ids of the newest rows that hold a cleansed copy
    of this cleaner version: the second thing `unchanged` needs. Asked for the postings that hold an item row only, in
    one statement for each `HEADS_AT_ONCE`. Reads only."""
    ids = [rows[-1].id for rows in postings if rows and any(h.kind == ITEM for h in rows)]
    out: set[uuid.UUID] = set()
    for at in range(0, len(ids), HEADS_AT_ONCE):
        out.update((await session.scalars(select(CleansedPosting.raw_id).where(
            CleansedPosting.owner == owner, CleansedPosting.cleaner_version == cleanse.CLEANER_VERSION,
            CleansedPosting.raw_id.in_(ids[at:at + HEADS_AT_ONCE])))).all())
    return out


def _copy(owner: str, use: list[Held], c: cleanse.Cleansed, now: datetime) -> CleansedPosting:
    """The cleansed copy `c` of the rows of `use`: its `raw_id` is the newest of them."""
    return CleansedPosting(
        id=uuid.uuid4(), owner=owner, raw_id=use[-1].id, cleaner_version=cleanse.CLEANER_VERSION,
        from_raw=[str(h.id) for h in use], title=c.title, employer=c.employer, locations=c.locations,
        work_mode=c.work_mode, pay_min=c.pay_min, pay_max=c.pay_max, posted_date=c.posted_date,
        text_clean=c.text_clean, removed=c.removed, marks=c.marks, partial=c.partial,
        no_raw=all(h.kind == LEGACY for h in use), cleaned_at=now)


def build(owner: str, rows: list[Held], now: datetime) -> CleansedPosting:
    """The cleansed copy of a posting from its raw rows in the order they were written: the newest of each kind, folded
    oldest to newest. Its `raw_id` is the newest of them."""
    use = _use(rows)
    return _copy(owner, use, _cleaned(use), now)


async def held(session: AsyncSession, owner: str, source_id: uuid.UUID, ext_id: str | None = None
               ) -> dict[str, list[Held]]:
    """Every raw row of a source (or of one posting of it) without its bytes, by posting id, each list in the order the
    rows were written."""
    q = select(RawPosting.id, RawPosting.ext_id, RawPosting.kind, RawPosting.body_sha256, RawPosting.parsed).where(
        RawPosting.owner == owner, RawPosting.source_id == source_id).order_by(RawPosting.seq)
    if ext_id is not None:
        q = q.where(RawPosting.ext_id == ext_id)
    out: dict[str, list[Held]] = {}
    for rid, ext, kind, sha, parsed in (await session.execute(q)).all():
        out.setdefault(ext, []).append(Held(rid, kind, sha, parsed or {}))
    return out


async def heads(session: AsyncSession, owner: str, source_id: uuid.UUID, ext_ids: Collection[str]
                ) -> dict[str, list[Held]]:
    """The raw rows of these postings of a source, by posting id, each list in the order the rows were written, without
    their bytes and without their parse (`Held.loaded` is False: `with_parse` reads it for the rows that need it). A
    board's rows with their parse are megabytes (each holds the posting's whole text); their heads are a few bytes."""
    out: dict[str, list[Held]] = {}
    wanted = sorted(ext_ids)
    for at in range(0, len(wanted), HEADS_AT_ONCE):
        q = select(RawPosting.id, RawPosting.ext_id, RawPosting.kind, RawPosting.body_sha256).where(
            RawPosting.owner == owner, RawPosting.source_id == source_id,
            RawPosting.ext_id.in_(wanted[at:at + HEADS_AT_ONCE])).order_by(RawPosting.seq)
        for rid, ext, kind, sha in (await session.execute(q)).all():
            out.setdefault(ext, []).append(Held(rid, kind, sha, {}, loaded=False))
    return out


async def with_parse(session: AsyncSession, postings: Iterable[list[Held]]) -> None:
    """Read the parse of the rows the cleaner folds (`_use`: the newest of each kind of each posting's rows) that
    `heads` left unread, into the rows. Nothing is read for the rest of a board's rows."""
    need = {h.id: h for rows in postings for h in _use(rows) if not h.loaded}
    ids = list(need)
    for at in range(0, len(ids), PARSED_AT_ONCE):  # a statement a page: the loop is given back between them
        q = select(RawPosting.id, RawPosting.parsed).where(RawPosting.id.in_(ids[at:at + PARSED_AT_ONCE]))
        for rid, parsed in (await session.execute(q)).all():
            need[rid].parsed, need[rid].loaded = parsed or {}, True


def forget_parse(rows: list[Held]) -> None:
    """Let go of the parse `with_parse` read into these rows (each is the posting's whole text): the cleaner has
    answered, and a copy made again reads it afresh."""
    for h in rows:
        h.parsed, h.loaded = {}, False


async def holds_board_page(session: AsyncSession, posting: JobPosting) -> bool:
    """Whether the text a posting holds is a job board's page as the app's Chrome read it (`board_page`): the posting's
    newest page read says the very text the posting holds, whatever rows came after it (a search that meets the posting
    again stores its card again when the card says another text; a card that moves only an applicant count is not kept,
    so rows after the page do not matter either way). A text another read put there (the employer's, copied on when the
    role was sourced) is not the page's, and a page read beside a text the posting already held says no text: neither
    is. Reads only."""
    newest = (await session.execute(select(RawPosting.body).where(
        RawPosting.owner == posting.owner, RawPosting.source_id == posting.employer_id,
        RawPosting.ext_id == posting.ext_id, RawPosting.reader == board_page())
        .order_by(RawPosting.seq.desc()).limit(1))).first()
    if newest is None:
        return False
    try:
        said = json.loads(inflate(newest.body))
    except ValueError:
        return False
    return isinstance(said, dict) and (said.get("text") or "").strip() == (posting.text or "").strip()


def _row(owner: str, source_id: uuid.UUID, posting_id: uuid.UUID | None, *, reader: str, reader_version: int | None,
         kind: str, ext_id: str, url: str | None, now: datetime, status: int | None, content_type: str | None,
         body: bytes, parsed: dict, packed: bytes | None = None, sha: str | None = None) -> RawPosting:
    """A raw row of `body`. `packed` (its deflated bytes) and `sha` (its hash) are given by a caller that made them
    already, before it held any lock (`prepare`)."""
    return RawPosting(
        id=uuid.uuid4(), owner=owner, source_id=source_id, posting_id=posting_id, reader=reader,
        reader_version=reader_version, kind=kind, ext_id=ext_id, url=url, fetched_at=now, http_status=status,
        content_type=(content_type or "")[:120] or None, body=deflate(body) if packed is None else packed,
        body_sha256=sha or hashlib.sha256(body).hexdigest(), body_bytes=len(body), parsed=parsed,
        no_raw=kind == LEGACY)


def _copy_made(session: AsyncSession, owner: str, postings: Iterable[JobPosting]) -> None:
    """The roles of these postings have a new copy of their text to settle on (`role.settle`) and to rank (`pool.owed`,
    the pull: `rank.unranked` takes the roles with a current copy): every copy made here is one of the writers that
    post, so a role is looked at the moment its text changes."""
    roles = [p.opportunity_id for p in postings if p.opportunity_id]
    queue.post(session, owner, queue.ROLE_SETTLE, roles, cause="copies.made")
    queue.post(session, owner, queue.POOL_OWED, [""] if roles else [], cause="copies.made")


async def _point(session: AsyncSession, owner: str, made: Iterable[tuple[uuid.UUID, str, uuid.UUID]]) -> None:
    """Point each posting at the copy just made for it: `made` is `(source id, posting id, copy id)`, the source and
    posting id naming the posting as the raw rows do (`JobPosting.employer_id`, `ext_id`). Call it after the copies are
    flushed. A posting with no row in the index (a raw row kept for a posting that was never indexed) has nothing to
    point."""
    rows = [{"b_source": source_id, "b_ext": ext_id, "b_copy": copy_id} for source_id, ext_id, copy_id in made]
    if not rows:
        return
    table = cast(Table, JobPosting.__table__)
    with queue.exempt("the pointer is written with the copy it names, whose writer posts (`_copy_made`)"):
        await session.execute(
            update(table).where(table.c.owner == owner, table.c.employer_id == bindparam("b_source"),
                                table.c.ext_id == bindparam("b_ext")).values(current_cleansed_id=bindparam("b_copy")),
            rows)


def _current_by_the_rows():
    """A posting's current copy as the rows say it is (a column of a statement on `JobPosting`): the cleansed copy of
    the newest raw row at the posting's address, at the highest cleaner version; nothing when that row has none."""
    newest = (select(RawPosting.id).where(
        RawPosting.owner == JobPosting.owner, RawPosting.source_id == JobPosting.employer_id,
        RawPosting.ext_id == JobPosting.ext_id).order_by(RawPosting.seq.desc()).limit(1)
        .correlate(JobPosting).scalar_subquery())
    return (select(CleansedPosting.id).where(CleansedPosting.raw_id == newest)
            .order_by(CleansedPosting.cleaner_version.desc()).limit(1).correlate(JobPosting).scalar_subquery())


async def repoint(session: AsyncSession, owner: str, only: Collection[uuid.UUID] | None = None) -> int:
    """Point every posting of the owner (or just the postings `only` names) at its current copy as the rows say it is:
    the cleansed copy of the posting's newest raw row at the highest cleaner version, and nothing for a posting whose
    newest raw row has none. The pointer is kept as copies are written (`_point`), so this is for a database filled
    some other way (the copy from the old engine), for the postings whose rows just came to them (`follow`) and for the
    check that the pointers are what the rows say. Returns how many postings it changed. Services flush; the caller
    commits."""
    copy = _current_by_the_rows()
    wanted = [JobPosting.owner == owner, JobPosting.current_cleansed_id.is_distinct_from(copy)]
    if only is not None:
        wanted.append(JobPosting.id.in_(list(only)))
    with queue.exempt("the pointer is worked out from the rows that name the copy (`repoint`): no loop reads it"):
        done = await session.execute(
            update(JobPosting).where(*wanted).values(current_cleansed_id=copy)
            .execution_options(synchronize_session=False))
    return int(done.rowcount or 0)  # pyright: ignore[reportAttributeAccessIssue]


async def follow(session: AsyncSession, owner: str, from_id: uuid.UUID, to_id: uuid.UUID,
                 went: Mapping[str, tuple[uuid.UUID, str]]) -> int:
    """The postings read under `from_id` are under `to_id` now (an employer merge or split): their raw rows go with
    them, so each posting still has its history and its current copy at its own address. `went` names each posting
    that moved, by the id it had under `from_id`: the posting and the id it has now (the same one, or the one it took
    because `to_id` had that id for another posting). All of a posting's rows travel together, so the copy its newest
    row has is still the copy of its rows. A posting that was folded into one `to_id` already had (both records read
    it) is not in `went` when that one holds rows of its own, and its rows stay where they are: a copy is made from
    the rows of one posting (`build`), so the copy of a folded posting's row says nothing of what the other's rows hold
    (its page's text), and the posting that stays would name it and lose that text. When the one that stays holds no
    row, `went` names it for the folded posting's id: it takes all of that posting's rows, so their copy is its own.

    Only a row's address is written, and the posting it names: never its bytes, and no row is deleted. A row whose bytes
    the new address holds already (the same kind and hash: the address holds one row of each) stays where it is. The
    postings are then pointed at their current copy as the rows say it is (`repoint`), and the role of one whose copy
    changed (the address held a row already, or the posting had no copy until it took a folded one's rows) is told, as
    for any new copy. Call it after the postings have moved.
    Returns how many raw rows moved. Services flush; the caller commits."""
    if not went or from_id == to_id:
        return 0
    old = sorted(went)
    rows: list[RawPosting] = []
    for at in range(0, len(old), HEADS_AT_ONCE):
        rows.extend((await session.scalars(select(RawPosting).options(load_only(
            RawPosting.id, RawPosting.source_id, RawPosting.ext_id, RawPosting.posting_id, RawPosting.kind,
            RawPosting.body_sha256)).where(RawPosting.owner == owner, RawPosting.source_id == from_id,
                                           RawPosting.ext_id.in_(old[at:at + HEADS_AT_ONCE])))).all())
    if not rows:
        return 0
    there = await heads(session, owner, to_id, {went[row.ext_id][1] for row in rows})
    taken = {(ext_id, h.kind, h.sha) for ext_id, held_rows in there.items() for h in held_rows}
    theirs: set[uuid.UUID] = set()  # the postings that had rows: one whose every row stayed is pointed again too
    moved = 0
    for row in sorted(rows, key=lambda r: r.seq):
        posting_id, ext_id = went[row.ext_id]
        theirs.add(posting_id)
        if (ext_id, row.kind, row.body_sha256) in taken:
            continue
        taken.add((ext_id, row.kind, row.body_sha256))
        row.source_id, row.ext_id, row.posting_id = to_id, ext_id, posting_id
        moved += 1
    await session.flush()
    ids = sorted(theirs, key=str)
    for at in range(0, len(ids), HEADS_AT_ONCE):
        some = ids[at:at + HEADS_AT_ONCE]
        changed = (await session.scalars(select(JobPosting.opportunity_id).where(
            JobPosting.owner == owner, JobPosting.id.in_(some),
            JobPosting.current_cleansed_id.is_distinct_from(_current_by_the_rows())))).all()
        if not changed:
            continue
        await repoint(session, owner, only=some)
        roles = [role_id for role_id in changed if role_id]
        queue.post(session, owner, queue.ROLE_SETTLE, roles, cause="copies.moved")
        queue.post(session, owner, queue.POOL_OWED, [""] if roles else [], cause="copies.moved")
    return moved


def _held_of(row: RawPosting) -> Held:
    return Held(row.id, row.kind, row.body_sha256, row.parsed)


async def keep_items(session: AsyncSession, owner: str, source_id: uuid.UUID,
                     pairs: list[tuple[JobPosting, dict, str | None]], *, reader: str, reader_version: int | None,
                     now: datetime) -> int:
    """Keep the item each of these postings was read from (`pairs`: the index row, the reader's posting with its `raw`,
    the company it is filed under) and its cleansed copy, for every one whose bytes are new and whose content changed
    (`unchanged`: a posting that holds an item and a copy of this cleaner version and gives the cleaner the same
    inputs keeps nothing). The index rows are already flushed. Returns how many raw rows were written. It is one
    transaction's work, in the caller's (a site's read, a job board's search); the night's board read saves its copies a
    slice at a time (`keep_prepared`). The raw rows are read as heads and a parse only for the rows a posting's copy is
    folded from, and the loop is given back between copies (`Cooperate`)."""
    items = [(p, listed, name, getattr(listed, "raw", None)) for p, listed, name in pairs]
    items = [i for i in items if i[3] is not None]
    if not items:
        return 0
    coop = Cooperate()
    have = await heads(session, owner, source_id, {posting.ext_id for posting, *_rest in items})
    fresh: list[tuple[JobPosting, dict, str | None, bytes, str, str]] = []  # the items whose bytes are not held
    for posting, listed, name, item in items:
        await coop()
        body, content_type = body_of(item)
        sha = hashlib.sha256(body).hexdigest()
        if is_new(have.setdefault(posting.ext_id, []), sha):
            fresh.append((posting, listed, name, body, content_type, sha))
    # What the cleaner would be given needs the parse of the rows a posting's copy is folded from, and which rows hold a
    # copy of this cleaner version.
    await with_parse(session, [have[p.ext_id] for p, *_rest in fresh if any(h.kind == ITEM for h in have[p.ext_id])])
    current = await current_copies(session, owner, [have[p.ext_id] for p, *_rest in fresh])
    made: list[tuple[str, RawPosting]] = []
    for posting, listed, name, body, content_type, sha in fresh:
        await coop()
        rows = have[posting.ext_id]
        parsed = known_day(rows, snapshot(listed, name))
        if not is_new(rows, sha) or unchanged(rows, parsed, current):
            continue
        row = _row(owner, source_id, posting.id, reader=reader, reader_version=reader_version, kind=ITEM,
                   ext_id=posting.ext_id, url=listed.get("url"), now=now, status=200, content_type=content_type,
                   body=body, parsed=parsed)
        session.add(row)
        rows.append(_held_of(row))
        made.append((posting.ext_id, row))
    await session.flush()  # the raw rows are there before the copies that point at them
    await with_parse(session, [have[ext_id] for ext_id, _written in made])
    pointers = []
    for ext_id, _written in made:
        await coop()
        copy = build(owner, have[ext_id], now)
        session.add(copy)
        pointers.append((source_id, ext_id, copy.id))
    await session.flush()
    await _point(session, owner, pointers)
    written = {ext_id for ext_id, _r in made}
    _copy_made(session, owner, [p for p, *_rest in items if p.ext_id in written])
    return len(made)


@dataclass
class Prepared:
    """What the item of one posting needs saved, made with no session and no lock (`prepare`): its bytes, their deflated
    bytes and hash, the plain parse, the ids of the posting's raw rows as they were read, and the cleaner's answer for
    the rows its copy is folded from, the new item among them. An item that changed nothing the cleaner reads
    (`unchanged`) is marked: it has no deflated bytes and no answer, and is saved only when the posting gained a row
    since (`keep_prepared`), by building the copy afresh."""

    ext_id: str
    body: bytes
    packed: bytes | None
    sha: str
    content_type: str
    url: str | None
    parsed: dict
    held: frozenset[uuid.UUID]
    cleaned: cleanse.Cleansed | None
    unchanged: bool = False


def item_bytes(item: Any) -> tuple[bytes, str, str]:
    """The bytes of an item a reader gave, their content type and their hash."""
    body, content_type = body_of(item)
    return body, content_type, hashlib.sha256(body).hexdigest()


def is_new(rows: list[Held], sha: str) -> bool:
    """Whether none of a posting's raw rows is an item of these very bytes: a read that gets the same bytes again
    writes nothing. (The first test of a list read; `unchanged` is the second, for bytes that are new.)"""
    return not any(h.kind == ITEM and h.sha == sha for h in rows)


def prepare(ext_id: str, listed: Any, name: str | None, body: bytes, content_type: str, sha: str, rows: list[Held],
            current: Collection[uuid.UUID] = ()) -> Prepared:
    """Everything of an item's copy that needs no session, made before the slice that saves it takes a lock: the
    deflated bytes, the plain parse and the cleaner's answer (14 ms a posting: most of a board's whole save) over the
    rows the copy is folded from, the new item among them. `rows` are the posting's raw rows as read, the parse of the
    ones the copy folds loaded (`with_parse`). An item that `unchanged` calls so (`current`: `current_copies`) is made
    no further than its mark: nothing is deflated and the cleaner does not run."""
    parsed = known_day(rows, snapshot(listed, name))
    held_then = frozenset(h.id for h in rows)
    if unchanged(rows, parsed, current):
        return Prepared(ext_id, body, None, sha, content_type, listed.get("url"), parsed, held_then, None,
                        unchanged=True)
    folded = [*rows, Held(uuid.UUID(int=0), ITEM, sha, parsed)]  # the new row: its own id is made when it is saved
    return Prepared(ext_id, body, deflate(body), sha, content_type, listed.get("url"), parsed, held_then,
                    _cleaned(_use(folded)))


async def keep_prepared(session: AsyncSession, owner: str, source_id: uuid.UUID,
                        pairs: list[tuple[JobPosting, Prepared]], *, reader: str, reader_version: int | None,
                        now: datetime) -> int:
    """Save the items `prepare` made for these postings (flushed, ids made) and the cleansed copies that follow, the raw
    rows first. Returns how many raw rows were written. The postings' raw rows are read again here, in one statement:
    a read of a posting's page or a hand-in may have kept a row since they were read. An item of these very bytes kept
    meanwhile is not kept again, and a posting that gained another row has its copy folded again from the rows it has
    now (`build`), not from the ones its item was prepared against. An item that was decided unchanged
    (`Prepared.unchanged`) is kept only for a posting that gained a row: the decision was made on the rows the posting
    held then, and is not true of others."""
    if not pairs:
        return 0
    now_held = await heads(session, owner, source_id, [p.ext_id for _posting, p in pairs])
    made: list[tuple[JobPosting, Prepared, list[Held]]] = []
    for posting, p in pairs:
        have = now_held.get(p.ext_id, [])
        if not is_new(have, p.sha):
            continue  # another read kept these bytes meanwhile
        if p.unchanged and frozenset(h.id for h in have) == p.held:
            continue  # it changed nothing the cleaner reads, and the posting holds the very rows it was decided on
        row = _row(owner, source_id, posting.id, reader=reader, reader_version=reader_version, kind=ITEM,
                   ext_id=p.ext_id, url=p.url, now=now, status=200, content_type=p.content_type, body=p.body,
                   parsed=p.parsed, packed=p.packed, sha=p.sha)
        session.add(row)
        made.append((posting, p, [*have, _held_of(row)]))
    await session.flush()  # the raw rows are there before the copies that point at them
    moved = {p.ext_id for _posting, p, rows in made
             if p.cleaned is None or frozenset(h.id for h in rows[:-1]) != p.held}
    await with_parse(session, [rows for _posting, p, rows in made if p.ext_id in moved])
    pointers = []
    for _posting, p, rows in made:
        answer = None if p.ext_id in moved else p.cleaned
        copy = build(owner, rows, now) if answer is None else _copy(owner, _use(rows), answer, now)
        session.add(copy)
        pointers.append((source_id, p.ext_id, copy.id))
    await session.flush()
    await _point(session, owner, pointers)
    _copy_made(session, owner, [posting for posting, _p, _rows in made])
    return len(made)


async def _keep_detail(session: AsyncSession, posting: JobPosting, more: object, now: datetime, *,
                       text: str | None) -> int:
    """Keep what a posting's own page answered (`more`, as `postings.read_detail` gave it, with the replies it was made
    from) and the cleansed copy that follows, which holds `text`. Only `postings.hold` calls it, after the answer was
    put on the posting. Nothing is written for an answer that came with no reply to keep. Returns how many raw rows."""
    replies = getattr(more, "replies", None) or []
    raw = getattr(more, "raw", None)
    if not isinstance(more, dict) or not (replies or raw is not None):
        return 0
    sent = [(body_of(raw)[0], "application/json", 200, posting.url)] if raw is not None else [
        (r.content, r.headers.get("content-type"), r.status_code, str(r.url)) for r in replies]
    return await _keep_sent(session, posting, sent, reader=f"adapter:{posting.ats}", kind=DETAIL,
                            parsed=of_posting(posting, text), now=now)


async def _keep_sent(session: AsyncSession, posting: JobPosting, sent: list[tuple], *, reader: str, kind: str,
                     parsed: dict, now: datetime, version: int | None = None) -> int:
    """Keep each `(bytes, content type, status, address)` of `sent` as a raw row of the posting (one the posting
    already has, by its hash, is not kept again) and, when any is new, the cleansed copy that follows."""
    owner, source_id = posting.owner, posting.employer_id
    rows = (await held(session, owner, source_id, posting.ext_id)).get(posting.ext_id, [])
    made = 0
    for body, content_type, status, url in sent:
        if any(h.kind == kind and h.sha == hashlib.sha256(body).hexdigest() for h in rows):
            continue
        row = _row(owner, source_id, posting.id, reader=reader, reader_version=version, kind=kind,
                   ext_id=posting.ext_id, url=url, now=now, status=status, content_type=content_type, body=body,
                   parsed=parsed)
        session.add(row)
        rows.append(_held_of(row))
        made += 1
    if made:
        await session.flush()
        copy = build(owner, rows, now)
        session.add(copy)
        await session.flush()
        await _point(session, owner, [(source_id, posting.ext_id, copy.id)])
        _copy_made(session, owner, [posting])
    return made


async def _keep_report(session: AsyncSession, posting: JobPosting, raw: Any, *, reader: str, text: str | None,
                       kind: str = ITEM, content_type: str | None = None, now: datetime | None = None) -> int:
    """Keep what a reader reported for a posting that no fetch of ours made: the owner's browser's item or page capture,
    a role the scout reported, a tracker row (`reader` `browser`, `browser:page`, `scout`, `import`). `raw` is what was
    reported, as it came (a dict is kept as JSON, a text as it is); `text` is the posting's text as it stands after the
    report. These rows are not made again from their bytes: `reparse.NOT_REPARSED`. Only `postings.hold` calls it.
    Returns how many raw rows."""
    body, made_type = body_of(raw)
    return await _keep_sent(session, posting, [(body, content_type or made_type, None, posting.url)], reader=reader,
                            kind=kind,
                            parsed=of_posting(posting, text), now=now or datetime.now(UTC))


async def fuller_text(session: AsyncSession, kept: JobPosting, other: JobPosting) -> str | None:
    """`other` is folded into `kept` (an employer merge: one posting, read under two records) and its rows stay where
    they are. When its current copy holds more text than `kept`'s (or `kept` has none), the text that copy was made
    from, as its rows gave it to the cleaner, for `keep_text` to enter on `kept`; None when `kept`'s is as long or
    longer. Nothing is written."""
    clean = {posting_id: text or "" for posting_id, text in (await session.execute(
        select(JobPosting.id, CleansedPosting.text_clean)
        .join(CleansedPosting, CleansedPosting.id == JobPosting.current_cleansed_id)
        .where(JobPosting.id.in_([kept.id, other.id])))).all()}
    theirs = clean.get(other.id, "")
    if len(theirs) <= len(clean.get(kept.id, "")):
        return None
    rows = (await held(session, other.owner, other.employer_id, other.ext_id)).get(other.ext_id, [])
    return cleaner_inputs(_use(rows)).get("text") or theirs


async def keep_text(session: AsyncSession, posting: JobPosting, text: str, now: datetime | None = None) -> int:
    """Enter `text` as the posting's newest copy, the way `keep_whole_text` enters a text the app holds: a `legacy` raw
    row at the posting's address, the cleansed copy of its rows with it, the posting pointed at that copy and its role
    told (`_keep_sent`). A text the posting holds already is not kept again (the hash). Returns how many raw rows."""
    return await _keep_sent(session, posting, [(text.encode("utf-8"), "text/plain", None, posting.url)],
                            reader=LEGACY, kind=LEGACY, parsed=of_posting(posting, text),
                            now=now or datetime.now(UTC))


async def keep_legacy(session: AsyncSession, owner: str, now: datetime | None = None,
                      role_ids: Iterable[uuid.UUID] | None = None) -> int:
    """Enter the postings that hold text and have no raw copy as a raw copy marked `no_raw` (the posting's stored text
    or its role's description, whichever is longer), each with its cleansed copy; with `role_ids`, only the postings of
    those roles. Returns how many postings."""
    now = now or datetime.now(UTC)
    done = 0
    wanted = list(role_ids) if role_ids is not None else None
    if wanted is not None and not wanted:
        return 0
    only = [JobPosting.opportunity_id.in_(wanted)] if wanted is not None else []
    while True:
        q = (select(JobPosting, Opportunity.description)
             .outerjoin(Opportunity, Opportunity.id == JobPosting.opportunity_id)
             .where(JobPosting.owner == owner, *only,
                    ~exists().where(RawPosting.owner == owner, RawPosting.source_id == JobPosting.employer_id,
                                    RawPosting.ext_id == JobPosting.ext_id),
                    or_(func.length(func.coalesce(JobPosting.text, "")) > 0,
                        func.length(func.coalesce(Opportunity.description, "")) > 0))
             .order_by(JobPosting.first_seen, JobPosting.id).limit(LEGACY_BATCH))
        batch = (await session.execute(q)).all()
        if not batch:
            break
        made: list[tuple[JobPosting, RawPosting]] = []
        for posting, description in batch:
            text = max((posting.text or "", description or ""), key=len)
            row = _row(owner, posting.employer_id, posting.id, reader=LEGACY, reader_version=None, kind=LEGACY,
                       ext_id=posting.ext_id, url=posting.url, now=posting.detail_at or posting.last_seen,
                       status=None, content_type="text/plain", body=text.encode("utf-8"),
                       parsed=of_posting(posting, text))
            session.add(row)
            made.append((posting, row))
        await session.flush()
        pointers = []
        for held_posting, row in made:
            copy = build(owner, [_held_of(row)], now)
            session.add(copy)
            pointers.append((held_posting.employer_id, held_posting.ext_id, copy.id))
        await session.flush()
        await _point(session, owner, pointers)
        _copy_made(session, owner, [p for p, _row in made])
        done += len(made)
    return done


async def keep_whole_text(session: AsyncSession, owner: str, role_ids: Iterable[uuid.UUID],
                          now: datetime | None = None) -> int:
    """For roles whose current cleansed copy is thin (a listing's snippet, read before the page was) while a posting of
    theirs now holds the whole text, or the role itself holds a whole description (`cleansed.MIN_TEXT` or more; never a
    browser posting that was never opened, whose text is a snippet), enter that text as the posting's newest copy, so
    the role takes it and moves on (`lifecycle.settle`). Whichever of the posting's text and the role's description is
    longer is kept, as `keep_legacy` does; a description is kept on one posting of
    the role, its newest. A copy already holding that text is not made again (the hash). Returns how many postings."""
    ids = list(role_ids)
    if not ids:
        return 0
    now = now or datetime.now(UTC)
    opened = or_(JobPosting.ats != ats.BROWSER, JobPosting.detail_at.is_not(None))

    def _bytes(column):  # the byte length: at least the character length, and read from the column's size alone
        return func.length(sa_cast(func.coalesce(column, ""), LargeBinary))

    # The candidates are found apart from the writer's turn, as ids only, by a test that every qualifying row passes
    # (a character is one byte or more); the turn loads those rows and applies the exact test.
    async with off_turn.reading(session) as reader:
        candidates = list((await reader.scalars(select(JobPosting.id).join(
            Opportunity, Opportunity.id == JobPosting.opportunity_id).where(
            JobPosting.owner == owner, JobPosting.opportunity_id.in_(ids),
            or_(and_(_bytes(JobPosting.text) >= cleansed.MIN_TEXT, opened),
                _bytes(Opportunity.description) >= cleansed.MIN_TEXT)))).all())
    if not candidates:
        return 0
    rows = (await session.execute(select(JobPosting, Opportunity.description).join(
        Opportunity, Opportunity.id == JobPosting.opportunity_id).where(
        JobPosting.owner == owner, JobPosting.opportunity_id.in_(ids), JobPosting.id.in_(candidates),
        or_(and_(func.length(func.coalesce(JobPosting.text, "")) >= cleansed.MIN_TEXT, opened),
            func.length(func.coalesce(Opportunity.description, "")) >= cleansed.MIN_TEXT))
        .order_by(JobPosting.first_seen.desc(), JobPosting.id))).all()
    done = 0
    described: dict[uuid.UUID | None, bool] = {}  # roles whose description is kept (not a set: `add` reads as a save)
    for posting, description in rows:
        own = (posting.text or "") if posting.ats != ats.BROWSER or posting.detail_at is not None else ""
        said = (description or "") if posting.opportunity_id not in described else ""
        text = said if len(said) > len(own) else own
        if len(text.strip()) < cleansed.MIN_TEXT:
            continue
        if text is said:
            described[posting.opportunity_id] = True
        done += bool(await _keep_sent(session, posting, [(text.encode("utf-8"), "text/plain", None, posting.url)],
                                      reader=LEGACY, kind=LEGACY, parsed=of_posting(posting, text), now=now))
    return done


async def _fresh(session: AsyncSession, owner: str, source_id: uuid.UUID, ext_id: str, rows: list[Held],
                 counts: dict) -> list[Held]:
    """The posting's raw rows with each newest row's plain posting made again from its bytes (`reparse.parsed`); a row
    that cannot be made again keeps the parse stored with it, and is counted by its reader in `counts["stored"]`."""
    from app.jobsearch.postings import reparse  # it reads this module's names

    full = list((await session.scalars(select(RawPosting).where(
        RawPosting.owner == owner, RawPosting.source_id == source_id, RawPosting.ext_id == ext_id)
        .order_by(RawPosting.seq))).all())
    details = [r for r in full if r.kind == DETAIL]
    newest = {r.kind: r for r in full}
    fresh: list[Held] = []
    for h in rows:
        row = newest.get(h.kind)
        if row is None or row.id != h.id:
            fresh.append(h)
            continue
        made = await reparse.parsed(session, row, details)
        if made is None:
            counts["stored"][row.reader.split(":")[0]] = counts["stored"].get(row.reader.split(":")[0], 0) + 1
        else:
            counts["reparsed"] += 1
        fresh.append(Held(h.id, h.kind, h.sha, made if made is not None else h.parsed))
    return fresh


async def recleanse(session: AsyncSession, owner: str, now: datetime | None = None,
                    only: set[tuple[uuid.UUID, str]] | None = None) -> dict:
    """Make the cleansed copy of every posting whose newest raw row has none at this cleaner version, from the raw rows
    alone: no fetch, no model. The reader's parse is run again on the raw bytes (`reparse.py`), so a better parse
    reaches the copy too; a raw row that cannot be parsed again (`reparse.NOT_REPARSED`) starts from the parse stored
    with it. Returns `{postings, rebuilt, reparsed, stored}`: `reparsed` counts the raw rows parsed again and `stored`
    those that were not, by reader. `only` limits it to these `(source id, posting id)` pairs."""
    now = now or datetime.now(UTC)
    if only is None:
        sources = (await session.scalars(
            select(RawPosting.source_id).where(RawPosting.owner == owner).distinct())).all()
    else:
        sources = sorted({source for source, _ext in only}, key=str)
    counts: dict = {"postings": 0, "rebuilt": 0, "reparsed": 0, "stored": {}}
    for source_id in sources:
        if only is None:
            have = await held(session, owner, source_id)
        else:
            # The heads of the batch's postings in one statement, then the parse of the rows the cleaner folds; the
            # postings keep the order of their ids, as when each was read on its own.
            found = await heads(session, owner, source_id, {ext for source, ext in only if source == source_id})
            have = {ext: found[ext] for ext in sorted(found)}
            await with_parse(session, have.values())
        current = set((await session.scalars(select(CleansedPosting.raw_id).where(
            CleansedPosting.owner == owner, CleansedPosting.cleaner_version == cleanse.CLEANER_VERSION,
            CleansedPosting.raw_id.in_([rows[-1].id for rows in have.values()])))).all())
        rebuilt: list[str] = []
        pointers = []
        for ext_id, rows in have.items():
            counts["postings"] += 1
            if rows[-1].id not in current:
                copy = build(owner, await _fresh(session, owner, source_id, ext_id, rows, counts), now)
                session.add(copy)
                pointers.append((source_id, ext_id, copy.id))
                counts["rebuilt"] += 1
                rebuilt.append(ext_id)
        await session.flush()
        await _point(session, owner, pointers)
        if rebuilt:  # the roles of the postings whose copy was made again
            roles = (await session.scalars(select(JobPosting.opportunity_id).where(
                JobPosting.owner == owner, JobPosting.employer_id == source_id, JobPosting.ext_id.in_(rebuilt),
                JobPosting.opportunity_id.is_not(None)))).all()
            queue.post(session, owner, queue.ROLE_SETTLE, roles, cause="copies.recleanse")
            queue.post(session, owner, queue.POOL_OWED, [""] if roles else [], cause="copies.recleanse")
    return counts


async def _cleans_otherwise(session: AsyncSession, owner: str, source_id: uuid.UUID, ext_id: str,
                            copy_id: uuid.UUID) -> bool:
    """Whether cleaning the posting again from its raw rows, as `recleanse` does (`_fresh`: each newest row parsed
    again from its bytes), gives another clean text than the copy `copy_id` holds. False too when that copy is not the
    copy of the posting's newest raw row at this cleaner version: `recleanse` would not put a copy in its place. Reads
    only."""
    rows = (await heads(session, owner, source_id, [ext_id])).get(ext_id, [])
    held_copy = (await session.execute(select(
        CleansedPosting.raw_id, CleansedPosting.cleaner_version, CleansedPosting.text_clean).where(
        CleansedPosting.owner == owner, CleansedPosting.id == copy_id))).one_or_none()
    if not rows or held_copy is None or (held_copy[0], held_copy[1]) != (rows[-1].id, cleanse.CLEANER_VERSION):
        return False
    await with_parse(session, [rows])
    fresh = await _fresh(session, owner, source_id, ext_id, rows, {"reparsed": 0, "stored": {}})
    return _cleaned(_use(fresh)).text_clean != held_copy[2]


async def heal_embedded(session: AsyncSession, owner: str, now: datetime | None = None) -> dict:
    """Make again the current copies that still hold embedded data (`cleanse.drop_embedded`, which came in under the
    same cleaner number: the comment above `cleanse.CLEANER_VERSION`). A copy made before the cleaner dropped it keeps
    the bytes for ever otherwise: a read of the same bytes makes no copy (`unchanged`), and `recleanse` makes one only
    where a raw row has none at this number.

    The postings whose current copy's clean text holds `data:` (in any case) are found on a reader; each is cleaned
    again from its raw rows as `recleanse` does, on a reader too, and only where the clean text comes out different
    is the old copy deleted (a raw row holds one copy for a cleaner number; a rank of the old text goes with it) and
    `recleanse` asked for that posting: it makes the copy, points the posting at it and tells its role, which is
    settled and ranked again. A copy whose text only says `data:` in its words is left as it is and its role is told
    nothing, so a second run makes nothing. The work is saved `HEAL_BATCH` postings at a time, each batch a short
    transaction of its own (`db.release_if_free`). A command run once with the app stopped (`cli.py`,
    `heal-embedded-copies`); no schedule calls it. Returns `{looked_at, rebuilt}`: the copies that hold `data:` and
    those made again."""
    now = now or datetime.now(UTC)
    async with off_turn.reading(session) as reader:
        found = [(source_id, ext_id, copy_id) for source_id, ext_id, copy_id in (await reader.execute(
            select(JobPosting.employer_id, JobPosting.ext_id, CleansedPosting.id)
            .join(CleansedPosting, CleansedPosting.id == JobPosting.current_cleansed_id)
            .where(JobPosting.owner == owner, func.lower(CleansedPosting.text_clean).contains("data:"))
            .order_by(JobPosting.employer_id, JobPosting.ext_id))).all()]
    await db.release_if_free(session)
    counts = {"looked_at": len(found), "rebuilt": 0}
    for at in range(0, len(found), HEAL_BATCH):
        stale: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        async with off_turn.reading(session) as reader:
            for source_id, ext_id, copy_id in found[at:at + HEAL_BATCH]:
                if await _cleans_otherwise(reader, owner, source_id, ext_id, copy_id):
                    stale[(source_id, ext_id)] = copy_id
        if stale:
            # through the session, row by row, as the clearing of old copies does: no statement on a copy
            for old in (await session.scalars(select(CleansedPosting).options(load_only(CleansedPosting.id)).where(
                    CleansedPosting.owner == owner, CleansedPosting.id.in_(list(stale.values()))))).all():
                await session.delete(old)
            await session.flush()
            counts["rebuilt"] += (await recleanse(session, owner, now, only=set(stale)))["rebuilt"]
        await db.release_if_free(session)
    return counts


async def _names_towns_again(session: AsyncSession, owner: str, source_id: uuid.UUID, ext_id: str,
                             copy_id: uuid.UUID) -> bool:
    """Whether the posting's current copy `copy_id` states no town (only a count or a placeholder) while its raw rows,
    folded as `recleanse` folds them (`_fresh`, then `_fold`), name some. False when that copy is not the copy of the
    posting's newest raw row at this cleaner version. Reads only."""
    rows = (await heads(session, owner, source_id, [ext_id])).get(ext_id, [])
    held_copy = (await session.execute(select(
        CleansedPosting.raw_id, CleansedPosting.cleaner_version, CleansedPosting.locations).where(
        CleansedPosting.owner == owner, CleansedPosting.id == copy_id))).one_or_none()
    if not rows or held_copy is None or (held_copy[0], held_copy[1]) != (rows[-1].id, cleanse.CLEANER_VERSION):
        return False
    if any(not says_no_place(p) for p in held_copy[2] or []):
        return False
    await with_parse(session, [rows])
    fresh = await _fresh(session, owner, source_id, ext_id, rows, {"reparsed": 0, "stored": {}})
    return any(not says_no_place(p) for p in _cleaned(_use(fresh)).locations)


async def heal_counted_places(session: AsyncSession, owner: str, now: datetime | None = None) -> dict:
    """Make again the current copies that state only a count of places ("2 Locations") while the posting's raw rows
    name the towns: the nightly list read of a job system gives the count after its page gave the towns, and the fold
    let the later count replace them (`_fold` keeps them now). Each such copy is made again from the kept raw rows,
    with no fetch and no model, as `recleanse` does; no raw row is deleted or edited, and a count larger than the towns
    named still stands. Only the old copy is deleted (a raw row holds one copy for a cleaner number) and `recleanse`
    asked for that posting: it makes the copy, points the posting at it and tells its role, which is settled again.
    A second run finds nothing. The work is saved `HEAL_BATCH` postings at a time, each batch a short transaction of
    its own (`db.release_if_free`). Returns `{looked_at, rebuilt}`: the current copies that state no town and those made
    again."""
    now = now or datetime.now(UTC)
    async with off_turn.reading(session) as reader:
        found = [(source_id, ext_id, copy_id) for source_id, ext_id, copy_id, places in (await reader.execute(
            select(JobPosting.employer_id, JobPosting.ext_id, CleansedPosting.id, CleansedPosting.locations)
            .join(CleansedPosting, CleansedPosting.id == JobPosting.current_cleansed_id)
            .where(JobPosting.owner == owner, CleansedPosting.cleaner_version == cleanse.CLEANER_VERSION)
            .order_by(JobPosting.employer_id, JobPosting.ext_id))).all()
            if places and all(says_no_place(p) for p in places)]
    await db.release_if_free(session)
    counts = {"looked_at": len(found), "rebuilt": 0}
    for at in range(0, len(found), HEAL_BATCH):
        stale: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        async with off_turn.reading(session) as reader:
            for source_id, ext_id, copy_id in found[at:at + HEAL_BATCH]:
                if await _names_towns_again(reader, owner, source_id, ext_id, copy_id):
                    stale[(source_id, ext_id)] = copy_id
        if stale:
            for old in (await session.scalars(select(CleansedPosting).options(load_only(CleansedPosting.id)).where(
                    CleansedPosting.owner == owner, CleansedPosting.id.in_(list(stale.values()))))).all():
                await session.delete(old)
            await session.flush()
            counts["rebuilt"] += (await recleanse(session, owner, now, only=set(stale)))["rebuilt"]
        await db.release_if_free(session)
    return counts


def has_raw(posting: type[JobPosting]):
    """The condition that a posting has a raw copy (any row of its source and posting id). The owner leads, so an
    owner-first index serves it (a raw row and its posting share the owner through the employer)."""
    return exists().where(RawPosting.owner == posting.owner, RawPosting.source_id == posting.employer_id,
                          RawPosting.ext_id == posting.ext_id)
