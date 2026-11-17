"""What a runbook's `read` says when the site is a plain feed (the generic-sites design, Part 1.3): a board's list is
one JSON answer (`request`), or one page that holds its list as a data block (`embedded`), or the same paged by a
number. The checks of that shape, and nothing else: pure data and standard library besides pydantic, so the schema can
ask them. `feed_read.py` runs what is checked here.

Code names the problem, never the site. Each item below is there because a feed does it and the plain fields could not:

  - a field's value (`Value`, below): a dotted path; a list of values, of which the first present is the field's (a
    posting dated by its first-published day, else its updated day); `join` (a place made of several fields); `html`
    (markup made plain text); `text` with `when` (a fixed word given when a yes/no field is true); `template` (an
    address built from the board's parts and the posting's id); `path` with `upper` (a country code in capitals);
    `blocks` (a text made of titled sections).
  - `skip`: rows that are not postings (an unlisted one, an internal one), left out of the list.
  - `work_mode`: the way of working from a written field, yes/no flags and the place's own words.
  - `comp`: the pay as the posting writes it, and its numbers from a structured pay object (one of a list, picked by
    what it says; cents; only a yearly range; only some currencies).
  - `paging`: by offset or page, stopped by the total the first answer states.
  - `detail`: one posting's own answer, or the list read again and the posting picked out of it, or the schema.org
    posting a page carries, for the fields the list left out.
  - board parts (`{slug}`, `{host}` and the parts named in `board_parts`) in the addresses.
  - `steps`: a value found on one page that is needed to ask the next (at most `STEPS_MAX` pages, each read from the
    runbook's own hosts, each value a short plain word sent only in an address on those hosts).
  - `post`: the list is asked for with a JSON body, the page number in it.
  - `headers`: what every request of the read says besides the usual.
  - a posting made of columns: `cell`, `cells` and `date_cell` read a row whose fields are the entries of one list and
    which says which entry is its title and which hold its places.
  - `unavailable`: a flag in the answer that says the board is shut, and so is no list.

Paths are dotted, as `skills._dotted` has them, and a number in one is a list position."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError, model_validator

#: One segment of a dotted path, as `skills._PATH_SEGMENT` has it.
_SEGMENT = re.compile(r"[A-Za-z0-9_$@:\- ]{1,80}")
_PART = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: The numbers a paged address may hold.
PAGING_NUMBERS = ("offset", "page", "size")
#: The names the read supplies itself in an address: the posting's id.
POSTING_PARTS = ("ext_id",)
#: The most values a `first`/`join` list, `blocks`, flags or `skip` may hold, and how deep values nest.
LIST_MAX = 12
DEPTH_MAX = 4
PATTERN_MAX = 200
PAGE_SIZE_MAX = 200
FIELDS = ("ext_id", "title", "location", "posted", "url", "apply_url", "text", "department")
DETAIL_FIELDS = ("text", "posted", "location", "apply_url", "department")
#: The most pages a read goes through before it asks for the list.
STEPS_MAX = 3
#: What a captured value must be: a short plain word (it goes into an address).
CAPTURED = re.compile(r"[A-Za-z0-9_.-]{1,100}")
#: What a `from_list` detail may take from the posting the list read made.
LIST_TAKES = ("text", "posted", "location", "work_mode", "comp", "comp_min", "comp_max", "apply_url", "department")


def path_ok(path: Any, what: str, allow_empty: bool = False) -> str:
    if not isinstance(path, str):
        raise ValueError(f"{what} is a dotted path")
    if not path:
        if allow_empty:
            return path
        raise ValueError(f"{what} is empty")
    if len(path) > PATTERN_MAX or not all(_SEGMENT.fullmatch(s) for s in path.split(".")):
        raise ValueError(f"{what} is not a dotted path ({path[:40]!r})")
    return path


def _parts_of(text: str) -> set[str]:
    return set(_PART.findall(text))


def check_value(spec: Any, what: str, depth: int = 0) -> None:
    """ValueError when `spec` is not a value the read understands (see the module text)."""
    if depth > DEPTH_MAX:
        raise ValueError(f"{what} is nested too deep")
    if isinstance(spec, str):
        path_ok(spec, what)
        return
    if isinstance(spec, list):
        if not spec or len(spec) > LIST_MAX:
            raise ValueError(f"{what}: a list of values holds 1 to {LIST_MAX}")
        for i, one in enumerate(spec):
            check_value(one, f"{what}[{i}]", depth + 1)
        return
    if not isinstance(spec, dict):
        raise ValueError(f"{what} is a path, a list of values or an object")
    keys = set(spec)
    forms = {
        "join": {"join", "sep"}, "html": {"html"}, "text": {"text", "when"}, "template": {"template"},
        "path": {"path", "upper"}, "blocks": {"blocks"}, "cell": {"cell"}, "cells": {"cells"},
        "date_cell": {"date_cell"}}
    form = next((f for f in forms if f in keys), None)
    if form is None or keys - forms[form]:
        raise ValueError(f"{what} has none of the forms of a value ({', '.join(forms)}), or has a key it does not take")
    if form == "join":
        parts = spec["join"]
        if not isinstance(parts, list) or not 1 <= len(parts) <= LIST_MAX:
            raise ValueError(f"{what}.join is a list of 1 to {LIST_MAX} values")
        for i, one in enumerate(parts):
            check_value(one, f"{what}.join[{i}]", depth + 1)
        sep = spec.get("sep", ", ")
        if not isinstance(sep, str) or len(sep) > 10:
            raise ValueError(f"{what}.sep is a short text")
    elif form == "html":
        check_value(spec["html"], f"{what}.html", depth + 1)
    elif form == "text":
        if not isinstance(spec["text"], str) or not spec["text"] or len(spec["text"]) > 100:
            raise ValueError(f"{what}.text is a short text")
        if "when" not in spec:
            raise ValueError(f"{what}: text goes with `when`, the yes/no field that gives it")
        path_ok(spec["when"], f"{what}.when")
    elif form == "template":
        text = spec["template"]
        if not isinstance(text, str) or not text or len(text) > 300:
            raise ValueError(f"{what}.template is a text")
        if text.count("{") != len(_PART.findall(text)) or text.count("}") != len(_PART.findall(text)):
            raise ValueError(f"{what}.template holds only {{name}} places")
    elif form == "path":
        path_ok(spec["path"], f"{what}.path")
        if "upper" in spec and not isinstance(spec["upper"], bool):
            raise ValueError(f"{what}.upper is true or false")
    elif form in ("cell", "cells", "date_cell"):
        _column_form(form, spec[form], f"{what}.{form}")
    elif form == "blocks":
        blocks = spec["blocks"]
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= LIST_MAX:
            raise ValueError(f"{what}.blocks is a list of 1 to {LIST_MAX} sections")
        for i, block in enumerate(blocks):
            if not isinstance(block, dict) or set(block) - {"at", "heading", "body"} or "at" not in block \
                    or "body" not in block:
                raise ValueError(f"{what}.blocks[{i}] is an object with at, body and optionally heading")
            for key in ("at", "heading", "body"):
                if key in block:
                    path_ok(block[key], f"{what}.blocks[{i}].{key}")


_COLUMN_KEYS = {"cell": {"of", "at"}, "cells": {"of", "at", "sep", "dashed_places"},
                "date_cell": {"of", "not_at", "not_in"}}


def _column_form(form: str, spec: Any, what: str) -> None:
    """A row whose fields are the entries of one list (`of`), told which entry is which by the row itself: `cell` is the
    entry at the position the row's field `at` holds (the first when it holds none); `cells` joins the entries at the
    positions of the list `at` holds (`dashed_places`: each is a place written "Country-State-City", alone or as a JSON
    list of them, and is read as "City, State, Country"); `date_cell` is the first entry that reads as a day, besides
    the positions the fields in `not_at` (one each) and `not_in` (a list each) hold."""
    if not isinstance(spec, dict) or set(spec) - _COLUMN_KEYS[form] or "of" not in spec:
        raise ValueError(f"{what} is an object with {', '.join(sorted(_COLUMN_KEYS[form]))}, and `of`")
    path_ok(spec["of"], f"{what}.of")
    if form in ("cell", "cells"):
        if "at" not in spec:
            raise ValueError(f"{what} has `at`, the field that says which entries")
        path_ok(spec["at"], f"{what}.at")
    if form == "cells":
        if not isinstance(spec.get("sep", "; "), str) or len(spec.get("sep", "; ")) > 10:
            raise ValueError(f"{what}.sep is a short text")
        if not isinstance(spec.get("dashed_places", False), bool):
            raise ValueError(f"{what}.dashed_places is true or false")
    if form == "date_cell":
        for key in ("not_at", "not_in"):
            paths = spec.get(key, [])
            if not isinstance(paths, list) or len(paths) > LIST_MAX:
                raise ValueError(f"{what}.{key} is a list of fields")
            for path in paths:
                path_ok(path, f"{what}.{key}")


def parts_in(spec: Any) -> set[str]:
    """Every `{name}` a value's templates hold."""
    if isinstance(spec, list):
        return {n for one in spec for n in parts_in(one)}
    if isinstance(spec, dict):
        found = set(_parts_of(spec["template"])) if isinstance(spec.get("template"), str) else set()
        for key in ("join", "html"):
            if key in spec:
                found |= parts_in(spec[key])
        return found
    return set()


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Skip(_Model):
    """A row that is not a posting: its field says this (`equals`: true, false or a word)."""

    path: str
    equals: StrictBool | StrictStr

    @model_validator(mode="after")
    def _plain(self) -> "Skip":
        path_ok(self.path, "skip.path")
        return self


class Flag(_Model):
    """A yes/no field of a posting that, when true, says how it is worked."""

    path: str
    mode: Literal["remote", "hybrid", "onsite"]

    @model_validator(mode="after")
    def _plain(self) -> "Flag":
        path_ok(self.path, "flags.path")
        return self


class WorkMode(_Model):
    """How a posting is worked: a `written` field the feed's own words are read from, yes/no `flags`, and, last, what
    the place's own words say. `flags_first`: the flags are asked before the written field."""

    written: Any = None
    flags: tuple[Flag, ...] = Field(default=(), max_length=LIST_MAX)
    flags_first: bool = False

    @model_validator(mode="after")
    def _plain(self) -> "WorkMode":
        if self.written is not None:
            check_value(self.written, "work_mode.written")
        return self


class PayRange(_Model):
    """A pay object: where it is (`path`, and for a list, `pick`: the entry whose fields say so), its numbers, and what
    makes them count: `unit` (a number of cents in a unit), `yearly_only` (a range that is not for a year gives no
    numbers), `currencies` (a range in another currency gives none)."""

    path: str
    pick: dict[str, str] | None = None
    min: str = "min"
    max: str = "max"
    currency: str | None = None
    period: str | None = None
    #: A period that is always the same (`year`), for a feed that says none.
    period_is: str | None = None
    unit: int | None = Field(None, ge=2, le=1000)
    yearly_only: bool = False
    currencies: tuple[str, ...] | None = None
    #: Pay written out from the numbers (`$100,000 - $120,000 per year`) rather than taken as the posting writes it.
    writes_text: bool = False

    @model_validator(mode="after")
    def _plain(self) -> "PayRange":
        path_ok(self.path, "comp.range.path", allow_empty=False)
        for name in ("min", "max", "currency", "period"):
            if getattr(self, name) is not None:
                path_ok(getattr(self, name), f"comp.range.{name}")
        for key, word in (self.pick or {}).items():
            path_ok(key, "comp.range.pick")
            if not isinstance(word, str) or not word:
                raise ValueError("comp.range.pick says a word for each field")
        return self


class PartTime(_Model):
    """Where a posting says it is part-time: its title and its kind of employment (an hourly range of a part-time role
    is no full-time year)."""

    title: str | None = None
    kind: str | None = None

    @model_validator(mode="after")
    def _plain(self) -> "PartTime":
        for name in ("title", "kind"):
            if getattr(self, name) is not None:
                path_ok(getattr(self, name), f"comp.part_time.{name}")
        return self


class Comp(_Model):
    """The pay of a posting: `written` as the posting says it, and, from `range`, its numbers. `parse_written`: the
    numbers are read from the written pay."""

    written: Any = None
    range: PayRange | None = None
    part_time: PartTime | None = None
    parse_written: bool = False

    @model_validator(mode="after")
    def _plain(self) -> "Comp":
        if self.written is not None:
            check_value(self.written, "comp.written")
        if self.range is None and not self.parse_written and self.written is None:
            raise ValueError("comp says the pay as written, or where its numbers are")
        if self.range is not None and self.parse_written:
            raise ValueError("comp takes its numbers from range or from the written pay, not both")
        if self.range is not None and not self.range.writes_text and self.written is None:
            raise ValueError("comp.range gives only numbers: say the pay as written, or `writes_text`")
        if self.range is not None and self.range.writes_text and self.written is not None:
            raise ValueError("comp.range writes the pay itself: take no written pay beside it")
        return self


class Paging(_Model):
    """The board is asked page by page: `param` is the number the address's `{offset}` or `{page}` holds, `size` what
    `{size}` holds, and `total` the dotted path, in the first answer, of how many postings the board holds (the read
    stops at it). A board that says none is read in the one page."""

    param: Literal["offset", "page"]
    size: int = Field(ge=1, le=PAGE_SIZE_MAX)
    total: str | None = None
    #: What the total is counted against: the pages read (`pages`: a page the size says it is), or the postings kept
    #: (`kept`: a board that does not say how many it gives a page).
    counts: Literal["pages", "kept"] = "pages"
    #: A first answer with no total is a changed shape, not a board of one page.
    total_required: bool = False

    @model_validator(mode="after")
    def _plain(self) -> "Paging":
        if self.total is not None:
            path_ok(self.total, "paging.total")
        if (self.counts == "kept" or self.total_required) and self.total is None:
            raise ValueError("paging.counts and total_required go with the paging's total")
        return self


class Detail(_Model):
    """What one posting's own answer adds to the list's. `from_list`: the feed has no answer for one posting, so the
    list is read again and the posting is picked out of it by its id (`take` names what is kept). Otherwise `url`
    (holding `{ext_id}`) is asked: a JSON answer (`at`: where its object is, `objects`: paths that must hold an object
    when they hold anything; `fields`) or, for `answer` `page_job_posting`, a page whose schema.org JobPosting is read
    (its description, date, pay, remote work and places)."""

    url: str | None = None
    from_list: bool = False
    take: tuple[str, ...] = ()
    answer: Literal["json", "page_job_posting", "page_encoded_list"] = "json"
    #: The posting's id may be any text (it is percent-encoded where the address holds it), not only a plain word.
    any_id: bool = False
    #: `page_encoded_list`: a page fills its description from a script list. `list_pattern` is the pattern, with one
    #: group, that finds the list in the page; `marker` begins each of its items that is text, which is URL-encoded.
    list_pattern: str | None = None
    marker: str | None = None
    at: str = ""
    objects: tuple[str, ...] = ()
    fields: dict[str, Any] = Field(default_factory=dict)
    work_mode: WorkMode | None = None
    comp: Comp | None = None

    @model_validator(mode="after")
    def _plain(self) -> "Detail":
        if self.from_list:
            if self.url is not None or self.fields or self.work_mode or self.comp or self.take == ():
                raise ValueError("a detail picked from the list has `take` and nothing else")
            if unknown := set(self.take) - set(LIST_TAKES):
                raise ValueError(f"detail.take has no {sorted(unknown)[0]!r} (use {', '.join(LIST_TAKES)})")
            return self
        if self.take:
            raise ValueError("take goes with from_list")
        if self.any_id and self.from_list:
            raise ValueError("any_id is for a detail that asks an address")
        if self.url is None:
            raise ValueError("a detail has a url, or is picked from the list")
        if "{ext_id}" not in self.url:
            raise ValueError("detail.url holds {ext_id}")
        if self.answer != "page_encoded_list" and (self.list_pattern is not None or self.marker is not None):
            raise ValueError("list_pattern and marker go with a page_encoded_list answer")
        if self.answer == "page_job_posting":
            if self.fields or self.work_mode or self.comp or self.at or self.objects:
                raise ValueError("a page's JobPosting is read whole: it takes no fields")
            return self
        if self.answer == "page_encoded_list":
            if self.fields or self.work_mode or self.comp or self.at or self.objects:
                raise ValueError("a page's encoded list is read whole: it takes no fields")
            if (not self.list_pattern or not self.marker or len(self.list_pattern) > PATTERN_MAX
                    or len(self.marker) > 10):
                raise ValueError("a page_encoded_list answer has a list_pattern and a marker")
            try:
                if re.compile(self.list_pattern).groups != 1:
                    raise ValueError("detail.list_pattern has exactly one group")
            except re.error as exc:
                raise ValueError(f"detail.list_pattern is not a pattern ({exc})") from None
            return self
        if self.at:
            path_ok(self.at, "detail.at")
        for path in self.objects:
            path_ok(path, "detail.objects")
        if not self.fields and not self.work_mode and not self.comp:
            raise ValueError("a detail says which fields it gives")
        for name, spec in self.fields.items():
            if name not in DETAIL_FIELDS:
                raise ValueError(f"detail.fields has no {name!r} (use {', '.join(DETAIL_FIELDS)})")
            check_value(spec, f"detail.fields.{name}")
        return self


class Step(_Model):
    """A page read before the list, for a value the list's address needs: `get` is its address (the board's parts and
    the values the steps before it captured go in it), `capture` says each value by the pattern, with one group, that
    finds it in the page. The page must be on a host under the runbook's own (a board that has been moved elsewhere is a
    404), and a value is a short plain word, put only in an address on those hosts, never in a host."""

    get: str
    capture: dict[str, str] = Field(min_length=1, max_length=LIST_MAX)

    @model_validator(mode="after")
    def _plain(self) -> "Step":
        for name, pattern in self.capture.items():
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", name) or name in ("slug", "host", *POSTING_PARTS, *PAGING_NUMBERS):
                raise ValueError(f"step.capture.{name} is not a name for a captured value")
            if not isinstance(pattern, str) or not pattern or len(pattern) > PATTERN_MAX:
                raise ValueError(f"step.capture.{name} is a pattern")
            try:
                if re.compile(pattern).groups != 1:
                    raise ValueError(f"step.capture.{name} has exactly one group")
            except re.error as exc:
                raise ValueError(f"step.capture.{name} is not a pattern ({exc})") from None
        return self


class Post(_Model):
    """The list is asked for with a JSON body: `body`, and in it, for each key of `numbers`, the number of the paging it
    names (`page`, `offset` or `size`); `headers` are the ones the list's request adds."""

    body: dict[str, Any]
    numbers: dict[str, Literal["offset", "page", "size"]] = Field(default_factory=dict, max_length=LIST_MAX)
    headers: dict[str, str] = Field(default_factory=dict, max_length=LIST_MAX)


class Unavailable(_Model):
    """A flag of the list's answer (`path`, true) that says the board is shut; the read then fails with `says`
    (`{who}` is the system's name)."""

    path: str
    says: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _plain(self) -> "Unavailable":
        path_ok(self.path, "unavailable.path")
        return self


class Read(_Model):
    """A plain feed's read (see the module text). Parts of an address that are neither numbers of the paging nor the
    posting's id are the board's: `slug`, `host` and any named in `board_parts`."""

    kind: Literal["request", "embedded"]
    url: str
    #: `embedded` only: where in the page the list is (`script:<id>`, `ldjson:<n>` or `var:<name>`).
    blob: str | None = None
    #: Dotted path to the list in the answer; "" when the answer is the list.
    items: str
    fields: dict[str, Any]
    skip: tuple[Skip, ...] = Field(default=(), max_length=LIST_MAX)
    work_mode: WorkMode | None = None
    comp: Comp | None = None
    paging: Paging | None = None
    detail: Detail | None = None
    #: Pages read before the list, for the values its address needs (at most `STEPS_MAX`).
    steps: tuple[Step, ...] = Field(default=(), max_length=STEPS_MAX)
    post: Post | None = None
    #: What every request of the read says besides the usual (a header name and its value).
    headers: dict[str, str] = Field(default_factory=dict, max_length=LIST_MAX)
    unavailable: Unavailable | None = None
    #: The board parts that are not `slug` or `host`: each with the pattern its value must match whole.
    board_parts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _plain(self) -> "Read":
        path_ok(self.items, "items", allow_empty=True)
        for name in self.fields:
            if name not in FIELDS:
                raise ValueError(f"fields has no {name!r} (use {', '.join(FIELDS)})")
        for needed in ("ext_id", "title"):
            if needed not in self.fields:
                raise ValueError(f"fields needs {needed}")
        for name, spec in self.fields.items():
            check_value(spec, f"fields.{name}")
        for name, pattern in self.board_parts.items():
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", name) or name in ("slug", "host", *POSTING_PARTS, *PAGING_NUMBERS):
                raise ValueError(f"board_parts.{name} is not a name for a board part")
            if not isinstance(pattern, str) or not pattern or len(pattern) > PATTERN_MAX:
                raise ValueError(f"board_parts.{name} is a pattern")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"board_parts.{name} is not a pattern ({exc})") from None
        names = {"slug", "host", *self.board_parts}
        captured: set[str] = set()
        if (self.steps or self.post or self.headers or self.unavailable) and self.kind != "request":
            raise ValueError("steps, post, headers and unavailable are for a request read")
        for i, step in enumerate(self.steps):
            if not step.get.startswith("https://"):
                raise ValueError(f"steps[{i}].get is an https address")
            if bad := _parts_of(step.get) - names - captured:
                raise ValueError(f"steps[{i}].get holds {{{sorted(bad)[0]}}}, which is no part of the board")
            if dup := set(step.capture) & (captured | names):
                raise ValueError(f"steps[{i}] captures {sorted(dup)[0]!r} twice")
            captured |= set(step.capture)
        paged = set(PAGING_NUMBERS) if self.paging else set()
        # A captured value is the list's own: no posting's address or field holds one (it is read before the list).
        for place, text in (("url", self.url), *((("detail.url", self.detail.url),)
                                                  if self.detail and self.detail.url else ())):
            ok = names | paged | ({"ext_id"} if place == "detail.url" else captured)
            if bad := _parts_of(text) - ok:
                raise ValueError(f"{place} holds {{{sorted(bad)[0]}}}, which is no part of the board")
        for name, spec in self.fields.items():
            if bad := parts_in(spec) - names - set(POSTING_PARTS):
                raise ValueError(f"fields.{name} holds {{{sorted(bad)[0]}}}, which is no part of the board")
        for place, text in (("url", self.url), *((f"steps[{i}].get", st.get) for i, st in enumerate(self.steps))):
            host = re.split(r"[/?#]", text.split("://", 1)[-1], maxsplit=1)[0]
            if "{" in host and not re.fullmatch(r"(?:\{(?:slug|host)\}|[a-z0-9.-])+", host):
                raise ValueError(f"only {{slug}} or {{host}} may be in the host of {place}")
        if not self.url.startswith("https://"):
            raise ValueError("url is an https address")
        sent = _parts_of(self.url) | (set(self.post.numbers.values()) if self.post else set())
        if self.paging and not (sent & set(PAGING_NUMBERS)):
            raise ValueError("a paged read holds {offset} or {page} in its url, or in its post's numbers")
        if self.paging and self.paging.param not in sent:
            raise ValueError(f"the paging is by {self.paging.param}: url or post holds it")
        if self.post and self.post.numbers and not self.paging:
            raise ValueError("post.numbers go with paging")
        if self.kind == "embedded":
            from app.sites import embedded

            if self.blob is None or not embedded.BLOB_NAME.fullmatch(self.blob):
                raise ValueError("blob names where the list is in the page: script:<id>, ldjson:<n> or var:<name>")
        elif self.blob is not None:
            raise ValueError("blob is for an embedded read")
        if self.detail is not None and self.detail.from_list and self.paging is not None:
            raise ValueError("a detail picked from the list is for a list that is one answer")
        return self


def check(read: Any, hosts: tuple[str, ...]) -> Read:
    """The `read` of a runbook that is a plain feed, checked; ValueError names what is wrong. Every address names a
    host of the runbook (or holds `{host}`/`{slug}` in its host: the board's own part, which is checked when it is
    read)."""
    try:
        got = Read.model_validate(read)
    except ValidationError as exc:
        err = exc.errors()[0]
        where = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        raise ValueError(f"{where}: {msg}" if where else msg) from None
    for place, text in (("url", got.url), *((f"steps[{i}].get", st.get) for i, st in enumerate(got.steps)),
                        *((("detail.url", got.detail.url),) if got.detail and got.detail.url else ())):
        host = re.split(r"[/?#]", text.split("://", 1)[-1], maxsplit=1)[0].lower()
        if "{host}" in host:
            continue
        concrete = host.replace("{slug}", "x")
        if not any(concrete == h or concrete.endswith("." + h) for h in hosts):
            raise ValueError(f"{place} is on a host that is none of the runbook's hosts")
    return got
