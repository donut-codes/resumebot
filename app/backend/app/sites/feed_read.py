"""Reading a plain feed from a runbook's `read` (`feed_schema.Read`): the board's list as one JSON answer or one page's
data block, paged when the feed pages, and, for the fields the list leaves out, one posting's own answer. The reader of
data that `feed_schema` checks; it is a job system's reader in everything the app asks of one (`read`, `parse_item`,
`detail`, `probe`) and raises the readers' own `AdapterError`, with the status the site answered with, so a failed read
is classed (retire or keep) as it always was (`postings.fault_kind`).

Nothing here names a site. The board's own parts (`{slug}`, `{host}`, the parts `board_parts` names) are put in an
address only after they are checked: a slug is quoted, a host is a plain host name under one of the runbook's hosts, any
other part matches the pattern the runbook gives. The posting's id is quoted where an address holds it."""

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote

from app.jobsearch.adapters._common import (
    AdapterError,
    Detailed,
    Listing,
    Pages,
    Posting,
    Probed,
    _answer,
    _changed,
    _comp,
    _date,
    _ext_id,
    _from_jsonld,
    _int,
    _join,
    _json_or_none,
    _jsonld,
    _kept,
    _loose_date,
    _mode,
    _page,
    _posting,
    _str,
    _yearly,
    html_to_text,
    parse_comp,
)
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.measures.pay_place import is_part_time
from app.jobsearch.postings.coop import Cooperate
from app.sites import embedded, feed_schema
from app.sites.schema import Probe, Runbook
from app.web_address import http_url

_PART = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_WORD = r"[A-Za-z0-9_.-]+"
NO_ADDRESS = "this employer's board has no address"

_compiled: dict[int, tuple[Runbook, feed_schema.Read | None]] = {}


def of(runbook: Runbook) -> "feed_schema.Read | None":
    """The plain-feed read of a runbook, checked; None when the runbook has no read, or one of another kind. Built once
    for each runbook (the runbook is held, so its identity stays its own)."""
    held = _compiled.get(id(runbook))
    if held is None:
        plain = runbook.read is not None and runbook.read.get("kind") in ("request", "embedded")
        held = (runbook, feed_schema.check(runbook.read, runbook.hosts) if plain else None)
        _compiled[id(runbook)] = held
    return held[1]


def _dig(value: Any, path: str) -> Any:
    from app.sites.skills import _dig as dig  # the one dotted-path reader (skills holds it with the recipe runner)

    return dig(value, path)


def _raw(v: Any) -> Any:
    return v if v is not None and v != "" else None


#: How each field is read from what the feed holds: its own words, or a day.
_READERS: dict[str, Callable[[Any], Any]] = {"posted": _date}


# --- the board's parts ----------------------------------------------------------------------------------------------


def parts_of(read: feed_schema.Read, board: dict, hosts: tuple[str, ...], scope: str = "list") -> dict[str, str]:
    """The board's parts that one piece of the read holds in its addresses, each checked: `slug` quoted, `host` a plain
    host name under one of the runbook's hosts, the others whole matches of their pattern. AdapterError (the board has
    no address) for one that is not. `scope` is the piece: `item` (what a list's item makes of a posting, which holds
    no fetch), `list` (the list's own address and its items) or `detail` (one posting's own address and answer). A
    board that lacks a part only another piece holds is read by this one all the same."""
    wanted: set[str] = set()
    if scope == "list":
        wanted |= set(_PART.findall(read.url))
        for step in read.steps:
            wanted |= set(_PART.findall(step.get))
    if scope == "detail" and read.detail:
        wanted |= set(_PART.findall(read.detail.url or ""))
        for spec in read.detail.fields.values():
            wanted |= feed_schema.parts_in(spec)
    if scope != "detail":
        for spec in read.fields.values():
            wanted |= feed_schema.parts_in(spec)
    out: dict[str, str] = {}
    taken = {name for step in read.steps for name in step.capture}
    for name in sorted(wanted - set(feed_schema.PAGING_NUMBERS) - set(feed_schema.POSTING_PARTS) - taken):
        found = board.get(name)
        if name == "slug":
            if not found or not isinstance(found, str):
                raise AdapterError(NO_ADDRESS)
            out[name] = quote(found, safe="")
        elif name == "host":
            if not isinstance(found, str) or not _HOST.fullmatch(found) or not any(
                    found.lower().endswith("." + h) for h in hosts):
                raise AdapterError(NO_ADDRESS)
            out[name] = found.lower()
        else:
            if not isinstance(found, str) or not re.fullmatch(read.board_parts.get(name, _WORD), found):
                raise AdapterError(NO_ADDRESS)
            out[name] = found
    return out


def _address(template: str, parts: dict[str, str], numbers: dict[str, Any] | None = None,
             ext_id: str | None = None) -> str:
    def put(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name == "ext_id":
            return quote(str(ext_id), safe="")
        if numbers and name in numbers:
            return str(numbers[name])
        return parts[name]

    return _PART.sub(put, template)


# --- one value ------------------------------------------------------------------------------------------------------


class _Ctx:
    """What a value may name beside the row: the board's checked parts and the posting's id."""

    def __init__(self, parts: dict[str, str], ext_id: str | None = None) -> None:
        self.parts = parts
        self.ext_id = ext_id


def value(spec: Any, row: Any, ctx: _Ctx, reader: Callable[[Any], Any] = _str) -> Any:
    """What a field's value (`feed_schema.check_value`) is in this row; None when it says nothing. The first present of
    a list is the first that `reader` finds something in."""
    if isinstance(spec, str):
        return reader(_dig(row, spec))
    if isinstance(spec, list):
        for one in spec:
            found = value(one, row, ctx, reader)
            if found:
                return found
        return None
    if "join" in spec:
        parts = [value(one, row, ctx, _str) for one in spec["join"]]
        return spec.get("sep", ", ").join(p for p in parts if p) or None
    if "html" in spec:
        got = value(spec["html"], row, ctx, lambda v: v if isinstance(v, str) else None)
        return html_to_text(got) or None
    if "text" in spec:
        return spec["text"] if _dig(row, spec["when"]) is True else None
    if "template" in spec:
        if "ext_id" in set(_PART.findall(spec["template"])) and not ctx.ext_id:
            return None
        return _address(spec["template"], ctx.parts, None, ctx.ext_id)
    if "path" in spec:
        got = reader(_dig(row, spec["path"]))
        return got.upper() if got and spec.get("upper") else got
    if "cell" in spec:
        return _cell(spec["cell"], row)
    if "cells" in spec:
        return _cells(spec["cells"], row)
    if "date_cell" in spec:
        return _date_cell(spec["date_cell"], row)
    if "blocks" in spec:
        blocks = []
        for block in spec["blocks"]:
            sec = _dig(row, block["at"])
            if isinstance(sec, dict):
                heading = _str(_dig(sec, block["heading"])) if "heading" in block else None
                body = _dig(sec, block["body"])
                blocks.append(_join(heading, html_to_text(body if isinstance(body, str) else None), sep="\n"))
        return _join(*blocks)
    return None


def _entries(spec: dict, row: Any) -> list:
    held = _dig(row, spec["of"])
    return held if isinstance(held, list) else []


def _position(at: Any) -> int:
    """Where the row says its entry is: a whole number, else the first."""
    return at if isinstance(at, int) else 0


def _cell(spec: dict, row: Any) -> str | None:
    """The entry the row's field `at` points to, as text."""
    entries = _entries(spec, row)
    at = _position(_dig(row, spec["at"]))
    return _str(entries[at]) if 0 <= at < len(entries) else None


def _dashed_places(raw: Any) -> str | None:
    """A place written "Country-State-City" (alone, or a JSON list of them) as people say it, "City, State, Country"."""
    try:
        names = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else [raw]
    except ValueError:
        names = [raw]
    places = [", ".join(reversed(n.split("-", 2))) for n in names if isinstance(n, str) and n.strip()]
    return "; ".join(places) or None


def _cells(spec: dict, row: Any) -> str | None:
    """The entries at the positions the row's list `at` holds, joined."""
    entries = _entries(spec, row)
    held = _dig(row, spec["at"])
    positions = [i for i in (held if isinstance(held, list) else []) if isinstance(i, int)]
    got = [_dashed_places(entries[i]) if spec.get("dashed_places") else _str(entries[i])
           for i in positions if 0 <= i < len(entries)]
    return spec.get("sep", "; ").join(g for g in got if g) or None


def _date_cell(spec: dict, row: Any) -> str | None:
    """The first entry that reads as a day, besides the positions the fields in `not_at` (one position each) and
    `not_in` (a list of them) hold."""
    skipped = {_position(_dig(row, path)) for path in spec.get("not_at", [])}
    for path in spec.get("not_in", []):
        held = _dig(row, path)
        skipped |= {i for i in held if isinstance(i, int)} if isinstance(held, list) else set()
    return next((d for i, c in enumerate(_entries(spec, row)) if i not in skipped and (d := _loose_date(c))), None)


def _field(fields: dict[str, Any], name: str, row: Any, ctx: _Ctx) -> Any:
    spec = fields.get(name)
    return None if spec is None else value(spec, row, ctx, _READERS.get(name, _str))


def work_mode(spec: feed_schema.WorkMode | None, row: Any, ctx: _Ctx, place: str | None) -> str | None:
    """remote, hybrid or onsite: from the flags (first, when `flags_first`), from the written field, from the flags,
    and last from the place's own words."""
    if spec is None:
        return None

    def flagged() -> str | None:
        return next((f.mode for f in spec.flags if _dig(row, f.path) is True), None)

    found = flagged() if spec.flags_first else None
    if found is None and spec.written is not None:
        found = _mode(value(spec.written, row, ctx, _raw), None)
    if found is None and not spec.flags_first:
        found = flagged()
    return found or _mode(None, place)


def _entry_counts(r: feed_schema.PayRange, entry: Any) -> bool:
    """Whether a pay object is the one that counts: it says what `pick` says, is for a year when it must be, and is in
    a currency that is allowed."""
    if not isinstance(entry, dict):
        return False
    if any(entry.get(k) != word for k, word in (r.pick or {}).items()):
        return False
    if r.yearly_only and not _yearly(_dig(entry, r.period) if r.period else r.period_is):
        return False
    return r.currencies is None or r.currency is None or _dig(entry, r.currency) in (None, *r.currencies)


def comp(spec: feed_schema.Comp | None, row: Any, ctx: _Ctx) -> tuple[str | None, int | None, int | None]:
    """(the pay as written, its lowest, its highest): the written pay, and numbers from a pay object or from the
    written pay."""
    if spec is None:
        return None, None, None
    written = value(spec.written, row, ctx, _str) if spec.written is not None else None
    if spec.range is None:
        low, high = parse_comp(written) if spec.parse_written else (None, None)
        return written, low, high
    r = spec.range
    found = _dig(row, r.path)
    entry = next((e for e in found if _entry_counts(r, e)), None) if isinstance(found, list) \
        else found if _entry_counts(r, found) else None
    if entry is None:
        return written, None, None
    low, high = _dig(entry, r.min), _dig(entry, r.max)
    if r.unit:
        whole_low, whole_high = _int(low), _int(high)
        low = whole_low // r.unit if whole_low else None
        high = whole_high // r.unit if whole_high else None
    if r.writes_text:
        part_time = False
        if spec.part_time is not None:
            part_time = is_part_time(_str(_dig(row, spec.part_time.title)) if spec.part_time.title else None,
                                     _str(_dig(row, spec.part_time.kind)) if spec.part_time.kind else None)
        return _comp(low, high, _dig(entry, r.currency) if r.currency else None,
                     _dig(entry, r.period) if r.period else r.period_is, part_time=part_time)
    return written, _int(low), _int(high)


# --- the list -------------------------------------------------------------------------------------------------------


def parse_item(read: feed_schema.Read, row: Any, board: dict, hosts: tuple[str, ...]) -> Posting | None:
    """One list item as the plain posting the feed makes of it (None: left out). Pure: the item is the raw copy, so the
    posting can be made again from it with no fetch."""
    ctx = _Ctx(parts_of(read, board, hosts, "item"))
    for skip in read.skip:
        got = _dig(row, skip.path)
        if type(got) is type(skip.equals) and got == skip.equals:
            return None
    ctx.ext_id = _field(read.fields, "ext_id", row, ctx)
    place = _field(read.fields, "location", row, ctx)
    text, low, high = comp(read.comp, row, ctx)
    return _posting(
        raw=row, ext_id=ctx.ext_id, title=_field(read.fields, "title", row, ctx), location=place,
        work_mode=work_mode(read.work_mode, row, ctx, place), comp=text, comp_min=low, comp_max=high,
        posted=_field(read.fields, "posted", row, ctx), url=_field(read.fields, "url", row, ctx),
        apply_url=_field(read.fields, "apply_url", row, ctx), text=_field(read.fields, "text", row, ctx),
        department=_field(read.fields, "department", row, ctx))


def _rows(answer: Any, items: str, who: str) -> list[dict]:
    rows = _dig(answer, items)
    if not isinstance(rows, list):
        raise _changed(who)
    good = [r for r in rows if isinstance(r, dict)]
    if rows and not good:
        raise _changed(who)
    return good


def _answer_of(read: feed_schema.Read, reply: Any, who: str) -> Any:
    if read.kind == "request":
        return _answer(reply, who)
    answer = embedded.extract(_page(reply, who), read.blob or "")
    if answer is None:
        raise _changed(who)
    return answer


async def _get(fetch: Fetcher, read: feed_schema.Read, url: str) -> Any:
    """One request of the read: the headers it says for all of them go with it."""
    return await fetch.get(url, headers=dict(read.headers)) if read.headers else await fetch.get(url)


async def _steps(fetch: Fetcher, read: feed_schema.Read, parts: dict[str, str], who: str,
                 hosts: tuple[str, ...]) -> dict[str, str]:
    """The values the pages before the list give (`feed_schema.Step`). A page answered from a host that is none of the
    runbook's is a board that has moved away: a 404. A value the page does not give, or that is not a short plain word,
    is a changed shape."""
    got: dict[str, str] = {}
    for step in read.steps:
        reply = await _get(fetch, read, _address(step.get, {**parts, **got}))
        host = (reply.url.host or "").lower()
        if not any(host.endswith("." + h) for h in hosts):
            raise AdapterError(f"{who} answered 404", 404)
        page = _page(reply, who)
        for name, pattern in step.capture.items():
            found = re.search(pattern, page)
            if found is None or not feed_schema.CAPTURED.fullmatch(found.group(1)):
                raise _changed(who)
            got[name] = found.group(1)
    return got


async def _ask(fetch: Fetcher, read: feed_schema.Read, url: str, numbers: dict[str, Any] | None) -> Any:
    """The list's request: a GET, or a POST of the read's JSON body with the paging's numbers in it."""
    if read.post is None:
        return await _get(fetch, read, url)
    body = {**read.post.body, **{key: (numbers or {})[name] for key, name in read.post.numbers.items()}}
    return await fetch.post(url, json=body, headers={**read.post.headers, **read.headers})


async def read_board(fetch: Fetcher, read: feed_schema.Read, board: dict, *, who: str,
                     hosts: tuple[str, ...]) -> Listing:
    """Every posting of the board: one answer, or page by page to the board's last (a page with no row, the total the
    first answer states, or, as every paged read, a page that repeats one read and the night's time running out)."""
    parts = parts_of(read, board, hosts)
    if read.steps:
        parts = {**parts, **await _steps(fetch, read, parts, who, hosts)}
    paging = read.paging
    pages = Pages(fetch)
    out: list[dict] = []
    seen = total = index = 0
    while True:
        numbers = {"offset": index * paging.size, "page": index + 1, "size": paging.size} if paging else None
        answer = _answer_of(read, await _ask(fetch, read, _address(read.url, parts, numbers), numbers), who)
        if read.unavailable is not None and _dig(answer, read.unavailable.path) is True:
            raise AdapterError(read.unavailable.says.replace("{who}", who))
        rows = _rows(answer, read.items, who)
        page: list[dict] = []
        coop = Cooperate()
        for row in rows:
            await coop()
            if (post := parse_item(read, row, board, hosts)) is not None:
                page.append(post)
        if paging is None:
            return Listing(_kept(page, len(rows), who))
        if index == 0 and paging.total:
            found = _int(_dig(answer, paging.total))
            if found is None and paging.total_required:
                raise _changed(who)
            total = found or 0
        seen += len(rows)
        out.extend(pages.fresh(page))
        index += 1
        if not rows or (len(out) if paging.counts == "kept" else index * paging.size) >= total or pages.stop():
            return Listing(_kept(out, seen, who), pages.cut, pages.note(len(out), total))


# --- one posting ----------------------------------------------------------------------------------------------------


async def read_detail(fetch: Fetcher, read: feed_schema.Read, board: dict, posting: dict, *, who: str,
                      hosts: tuple[str, ...]) -> dict:
    """What one posting's own answer adds to the list's: the fields to update, only those it has."""
    detail = read.detail
    assert detail is not None
    if detail.any_id:
        if not posting.get("ext_id"):
            raise AdapterError(f"{who} posting has no id")
        ext_id = str(posting["ext_id"])
    else:
        ext_id = _ext_id(posting, who)
    if detail.from_list:  # no answer for one posting: the board is read and the posting is picked out of it
        found = next((p for p in (await read_board(fetch, read, board, who=who, hosts=hosts)).postings
                      if p["ext_id"] == ext_id), None)
        update = {k: found.get(k) for k in detail.take} if found else {}
        picked = Detailed({k: v for k, v in update.items() if v})
        picked.raw = getattr(found, "raw", None)  # the answer is the whole board: the raw copy is this posting's item
        return picked
    assert detail.url is not None
    parts = parts_of(read, board, hosts, "detail")
    reply = await _get(fetch, read, _address(detail.url, parts, None, ext_id))
    if detail.answer == "page_encoded_list":
        return _from_encoded_list(detail, _page(reply, who), who)
    if detail.answer == "page_job_posting":
        ld = _jsonld(_page(reply, who))
        if ld is None:
            raise _changed(who)
        return _from_jsonld(ld, posting)
    answer = _answer(reply, who)
    obj = _dig(answer, detail.at) if detail.at else answer
    if not isinstance(obj, dict):
        raise _changed(who)
    for path in detail.objects:
        held = _dig(obj, path)
        if held and not isinstance(held, dict):
            raise _changed(who)
    ctx = _Ctx(parts, ext_id)
    update: dict[str, Any] = {name: value(spec, obj, ctx, _READERS.get(name, _str))
                              for name, spec in detail.fields.items()}
    if "apply_url" in update:
        update["apply_url"] = http_url(update["apply_url"])
    if detail.work_mode is not None:
        update["work_mode"] = work_mode(detail.work_mode, obj, ctx, update.get("location"))
    if detail.comp is not None:
        update["comp"], update["comp_min"], update["comp_max"] = comp(detail.comp, obj, ctx)
    return {k: v for k, v in update.items() if v}


def _from_encoded_list(detail: feed_schema.Detail, page: str, who: str) -> dict:
    """The text a page fills in from a script list: its items that begin with the marker, each URL-encoded, made plain
    text and joined."""
    assert detail.list_pattern is not None and detail.marker is not None
    filled = re.search(detail.list_pattern, page, re.DOTALL)
    if filled is None:
        raise _changed(who)
    marker = detail.marker
    items = re.findall(rf"'({re.escape(marker)}(?:[^'\\]|\\.)*)'", filled.group(1))
    text = _join(*[html_to_text(unquote(item[len(marker):])) for item in items])
    if text is None:
        raise _changed(who)
    return {"text": text}


# --- is there a board by this name ----------------------------------------------------------------------------------


async def probe(fetch: Fetcher, spec: Probe, slug: str) -> Probed | None:
    """Ask the feed whether a board named `slug` has postings (`Probe.url`): `Probed`, with the employer's name when the
    feed gives one, or None when there is no such board or the feed refused, failed or answered with anything else."""
    if spec.url is None:
        return None
    data = await _json_or_none(fetch, spec.url.replace("{slug}", slug))
    if not isinstance(data, dict):
        return None
    if spec.present is not None:
        held = _dig(data, spec.present)
        if not isinstance(held, list) or not held:
            return None
    return Probed(_str(_dig(data, spec.name)) if spec.name else None)
