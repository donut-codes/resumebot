"""A site's skill (docs/PRD.md path 29.2): the saved recipe the app runs to read one site's postings, and the runner.

A recipe is data only, never code, so the app can run it every night with nothing to trust but the checks below. It is
one of five kinds (a JSON dict with a `kind`; anything else is refused with the reason):

- `adapter`   a known job system's reader (`adapters.ADAPTERS`): `{"kind": "adapter", "ats": "<job system>",
              "board": {"slug": "..."}}`.
- `request`   the data request the site's own page makes, replayed: a GET or POST to a public address, the dotted path
              to the list in the JSON answer, and a dotted path for each field of a posting; optionally paged (by
              page, offset or cursor), with query `params` and a body that may hold the placeholders `{page}`,
              `{offset}`, `{cursor}` and `{size}` (a closed set), a `transforms` list for any field (a closed list,
              `recipe_ops.py`), an optional `detail` request for each posting's description, and `limits` and
              `provenance`. Still data: nothing a model wrote is ever run.
- `list_page` plain HTML list pages: the links whose address matches `link_pattern` are the postings, the link's text
              is the title, and `next_pattern` finds the next page.
- `page`      steps and selectors recorded once in the app's own Chrome (`jobsearch/browser/recipes/page_run.py`), run
each night by
              code over DevTools with no model: the owner's target terms typed in the site's search box, the result
              pages walked by the next control, the posting links listed, and a posting's page read for its text. Only
              CSS selectors (`css`), never script; run in the app's Chrome, not through the fetcher.
- `html`      selectors recorded from the rendered page and run on the page a plain fetch gets, no browser at all
              (docs/PRD.md path 49, `jobsearch/browser/recipes/html_run.py`): the start page's posting anchors (`links`,
              inside
              a `row` with its `location` and `date`), the next-page anchor and a posting page's text element. Only
              selectors of the closed grammar of `html_select.py`; read through the fetcher like the kinds above.
- `html_posting` the same for one employer host's single-posting pages, which have no site row: `host`, `url_pattern`
              and the `text` selector, or `jsonld: true` for a page whose text is the schema.org JobPosting it embeds.
- `browser`   how the owner's own browser session reads a site it must open in a browser (docs/PRD.md 37.1): where the
              search is, how a results row is laid out, where a posting's text is. Plain words for a reader to follow,
              not for code to run: the app only checks and keeps it, and `run_recipe` refuses it (the night's read
              leaves such a site alone).

Everything is fetched through `polite.Fetcher` (robots.txt, pacing, a refusal is a stop). Only public addresses are
ever requested (no local or private hosts, no sign-in), page sizes are capped, and a pattern is refused if it could
backtrack without end (nested repeats, a choice inside a repeat, a back-reference, a look-around), is limited to 300
characters, is only run on short text, and the whole run stops if matching eats more than a time budget.

A site is read to its last page, however many that is (docs/PRD.md 25.12): there is no cap on pages or postings. A read
stops early only when it must, and then says so and marks its listing truncated, so nothing missing from it is taken as
closed: the pages are going round in circles (a page brings nothing not already read), or the time the fetcher was given
has run out (`polite.timeup`: the night's read, 28.17, or a scout's quick try).

A skill is a `SiteSkill` row: one per version, kept, never overwritten. `current_skill` is the newest version that is
not retired; `save_skill` validates a recipe and writes the next version. Services flush; the route commits.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from re import _constants as _c
from re import _parser
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import regex
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.security import find_secret
from app.jobsearch import ats
from app.jobsearch.adapters import (
    ADAPTERS,
    ENOUGH_READ,
    PAGE_REPEATED,
    TIME_RAN_OUT,
    AdapterError,
    Listing,
    _loose_date,
    _mode,
    _posting,
    html_to_text,
    page_facts,
    parse_comp,
    stopped_at,
)
from app.jobsearch.fetch import polite
from app.jobsearch.fetch.polite import Fetcher, public_url, timeup
from app.sites import addresses, embedded, html_select, recipe_ops
from app.sites.models import Site, SiteSkill

KINDS = ("adapter", "request", "embedded", "list_page", "browser", "posting", "page", "html", "html_posting")

#: Limits. A recipe with a bigger number is refused, and a run never goes past them. (How many pages are read is not
#: one: a site is read to its last page.)
PATTERN_MAX = 300
PAGE_SIZE_MAX = 200
HTML_MAX = 2_000_000
#: How long a scout's try of a recipe (`skill.try`) and the Check's read of the saved one may take, in seconds: the
#: quick look at a site that has a great many pages stops here, truncated, instead of holding the session until the
#: last page.
TRY_SECONDS = 180.0
#: The postings a test of a recipe needs to read: it stops once a page brings this many (never the whole site).
VERIFY_ROWS = 3
#: Text a pattern is run on is cut to this (an address, a link's words).
MATCH_TEXT_MAX = 300
#: Total seconds a run may spend matching patterns before it stops.
REGEX_BUDGET_SECONDS = 2.0
TOO_SLOW = "a pattern took too long to match: the read was stopped"
RECIPE_MAX_BYTES = 8_000
#: A browser recipe is words: each text, and the whole recipe as saved (JSON), have a ceiling, and a few short notes.
BROWSER_TEXT_MAX = 300
BROWSER_RECIPE_MAX = 6_000
#: A runbook recipe (the Pathfinder's, written to browser.pathfinder.runbook.template.md): the runbook text is markdown
#: of at most this many characters, refused loudly above it, and the whole recipe (runbook, bookmarks and the rest, as
#: saved) has room beside it. The older shape (no runbook) keeps `BROWSER_RECIPE_MAX`.
BROWSER_RUNBOOK_MAX = 6_000
BROWSER_RUNBOOK_RECIPE_MAX = 16_000
BROWSER_BOOKMARKS_MAX = 30
BROWSER_RUNBOOK_FIELDS = ("runbook", "bookmarks", "ats", "new_since")
BROWSER_NOTES_MAX = 10
#: What the scout is told when it tries or keeps a browser recipe (message, how to fix): a browser recipe is the owner's
#: browser session's to write and try (`jobsearch/browser/reader.py`), never a project's.
BROWSER_REFUSAL = (
    "a browser skill can't be tried here: it is tried by the owner's browser session, not by the app",
    "never write a browser skill: a page that only loads in a browser is the browser's to read; call "
    "task.escalate with the reason needs_browser (resumebot hands the site to the owner's browser)",
)

#: `company` is for a job board or association, which lists other employers' roles; an employer's own site needs none.
FIELDS = ("title", "url", "location", "posted", "summary", "ext_id", "department", "comp", "company")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_$@:\- ]{1,80}")
_PARAM = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-\[\]]{0,40}")


class SkillError(Exception):
    """A saved recipe could not be run: the site answered with an error, or no longer has the shape the recipe reads.
    `status`: the HTTP status it answered with, when it answered one (`postings.fault_kind` classes a failed read by
    it)."""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --- addresses -------------------------------------------------------------------------------------------------------
# `public_url` (imported above) is the fetcher's own test of an address: a skill is only ever allowed to read these.


def _need_public(url: str, what: str) -> str:
    if not public_url(url):
        raise ValueError(f"{what} must be a public http(s) address (no sign-in, no local or private host)")
    return url


# --- patterns --------------------------------------------------------------------------------------------------------


#: How many long repeats (`+`, `*`, `{11,}`) one pattern may hold: each adds a power to the worst case.
WIDE_REPEATS_MAX = 3
_SubPattern = _parser.SubPattern
_REPEATS = tuple(getattr(_c, name) for name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT") if hasattr(_c, name))


def _subpatterns(av: Any):
    if isinstance(av, _SubPattern):
        yield av
    elif isinstance(av, (tuple, list)):
        for part in av:
            yield from _subpatterns(part)


def _walk(parsed, counted: list[int], in_wide_repeat: bool = False) -> None:
    for op, av in parsed.data:
        if op in (_c.GROUPREF, _c.GROUPREF_EXISTS):
            raise ValueError("a back-reference")
        if op in (_c.ASSERT, _c.ASSERT_NOT):
            raise ValueError("a look-around")
        wide = False
        if op in _REPEATS:
            wide = av[1] > 10
            if wide and in_wide_repeat:
                raise ValueError("a repeat inside a repeat")
            if wide:
                counted[0] += 1
                if counted[0] > WIDE_REPEATS_MAX:
                    raise ValueError(f"more than {WIDE_REPEATS_MAX} long repeats")
        if op == _c.BRANCH and in_wide_repeat:
            raise ValueError("a choice inside a repeat")
        for sub in _subpatterns(av):
            _walk(sub, counted, in_wide_repeat or wide)


def compile_pattern(pattern: str, what: str = "pattern") -> "regex.Pattern":
    """A pattern that is safe to run: at most PATTERN_MAX characters, and none of the shapes that can backtrack
    without end. ValueError names the problem. The shape check is a first line; the pattern it returns is the `regex`
    module's, which `_Budget.search` runs with a timeout, because the shape check cannot see every pattern that is slow
    on some text (`(?:a?){40}a{40}b`) and `re` cannot be stopped once it is matching."""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{what} is empty")
    if len(pattern) > PATTERN_MAX:
        raise ValueError(f"{what} is longer than {PATTERN_MAX} characters")
    try:
        parsed = _parser.parse(pattern, re.IGNORECASE)
        _walk(parsed, [0])
        re.compile(pattern, re.IGNORECASE)  # the friendly errors are `re`'s
        return regex.compile(pattern, regex.IGNORECASE)
    except (re.error, regex.error) as e:
        raise ValueError(f"{what} is not a valid pattern ({e})") from None
    except ValueError as e:
        raise ValueError(f"{what} is not allowed: {e}") from None
    except RecursionError:
        raise ValueError(f"{what} is too deeply nested") from None


class _Budget:
    """Runs patterns on short text and stops the run when matching has taken too long in all."""

    def __init__(self) -> None:
        self.spent = 0.0

    def search(self, pattern: "regex.Pattern", text: str) -> bool:
        left = REGEX_BUDGET_SECONDS - self.spent
        if left <= 0:
            raise SkillError(TOO_SLOW)
        started = time.perf_counter()
        try:
            found = pattern.search(text[:MATCH_TEXT_MAX], timeout=left) is not None
        except TimeoutError:
            raise SkillError(TOO_SLOW) from None
        self.spent += time.perf_counter() - started
        if self.spent > REGEX_BUDGET_SECONDS:
            raise SkillError(TOO_SLOW)
        return found

    def match(self, pattern: "regex.Pattern", text: str, group: int = 1) -> str | None:
        """The group of the first match in the short text, or None; the same budget and timeout as `search`."""
        left = REGEX_BUDGET_SECONDS - self.spent
        if left <= 0:
            raise SkillError(TOO_SLOW)
        started = time.perf_counter()
        try:
            found = pattern.search(text[:MATCH_TEXT_MAX], timeout=left)
        except TimeoutError:
            raise SkillError(TOO_SLOW) from None
        self.spent += time.perf_counter() - started
        if self.spent > REGEX_BUDGET_SECONDS:
            raise SkillError(TOO_SLOW)
        if found is None or group > (pattern.groups or 0):
            return None
        return (found.group(group) or "").strip() or None


# --- the recipe ------------------------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _dotted(path: str, what: str, allow_empty: bool = False) -> str:
    if not isinstance(path, str):
        raise ValueError(f"{what} must be a dotted path")
    if not path:
        if allow_empty:
            return path
        raise ValueError(f"{what} is empty")
    if len(path) > 200 or not all(_PATH_SEGMENT.fullmatch(seg) for seg in path.split(".")):
        raise ValueError(f"{what} is not a dotted path ({path[:40]!r})")
    return path


def adapter_systems() -> list[dict[str, Any]]:
    """The job systems an `adapter` recipe may name, each as `{key, name, parts}`: the key to write in `ats`, and the
    parts of a board that `board` must hold for that system. Sent to the learner beside the recipe template."""
    return [{"key": key, "name": ats.label(key), "parts": list(ats.SYSTEMS[key].needs)}
            for key in sorted(ADAPTERS) if key in ats.SYSTEMS]


class AdapterRecipe(_Model):
    kind: Literal["adapter"]
    ats: str
    board: dict[str, str | int | bool]

    @field_validator("ats")
    @classmethod
    def _known_system(cls, v: str) -> str:
        if v not in ats.SYSTEMS:
            raise ValueError(f"{v!r} is not a job system the app knows")
        if v not in ADAPTERS:
            raise ValueError(f"{ats.label(v)} has no reader yet")
        return v

    @model_validator(mode="after")
    def _complete(self):
        if len(self.board) > 20 or any(len(str(v)) > 200 for v in self.board.values()):
            raise ValueError("board is too big")
        if not ats.complete(self.ats, self.board):
            raise ValueError(f"board lacks the parts a {ats.label(self.ats)} reader needs: "
                             f"{', '.join(ats.SYSTEMS[self.ats].needs)}")
        # A reader whose board parts become the host the app connects to says when they are not plain words.
        problem = getattr(ADAPTERS[self.ats], "board_problem", lambda board: None)(dict(self.board))
        if problem:
            raise ValueError(problem)
        return self


class Paging(_Model):
    #: `offset` counts postings already read (0, size, 2 * size ...); `page` counts pages from 1.
    param: Literal["offset", "page", "cursor"]
    #: The page size asked for. Pages are asked for until the site's last (a short or an empty one).
    size: int = Field(ge=1, le=PAGE_SIZE_MAX)
    #: Where the number goes: in the address's query, or in the body (POST).
    where: Literal["query", "body"] = Field(default="query", alias="in")
    #: The name the site uses for it, when it is not `offset` or `page`, and the name it uses for the size, if it
    #: takes one.
    name: str | None = None
    size_name: str | None = None
    #: `cursor` paging: the dotted path, in each answer, of the cursor that asks for the next page (none: the last).
    cursor_path: str | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _cursor_consistent(self):
        if (self.param == "cursor") != (self.cursor_path is not None):
            raise ValueError("cursor_path goes with param cursor, and param cursor needs cursor_path")
        if self.cursor_path is not None:
            _dotted(self.cursor_path, "page.cursor_path")
        return self

    @field_validator("name", "size_name")
    @classmethod
    def _plain_name(cls, v: str | None) -> str | None:
        if v is not None and not _PARAM.fullmatch(v):
            raise ValueError("a parameter name is plain letters, digits, _ . - [ ]")
        return v


#: What a recipe's address, `params` and `body` may hold between braces: the paging's numbers, nothing else.
PLACEHOLDERS = ("page", "offset", "cursor", "size")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
#: The most requests a recipe may allow itself, and the most `params` it may carry.
REQUESTS_MAX = 5_000
PARAMS_MAX = 20
#: How a recipe's run is told to have stopped at its own request limit.
LIMIT_REACHED = "the recipe's request limit was reached"


def _placeholders(value: Any) -> set[str]:
    """Every `{name}` in a text, or in the texts of a list or object."""
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, dict):
        return {n for v in value.values() for n in _placeholders(v)}
    if isinstance(value, list):
        return {n for v in value for n in _placeholders(v)}
    return set()


#: How many fields a posting's text may be joined from.
PARTS_MAX = 8


class DetailPart(_Model):
    """One field of a posting's text: a dotted path in the answer and, optionally, the heading written above it."""

    path: str
    heading: str | None = Field(default=None, max_length=80)

    @field_validator("path")
    @classmethod
    def _path_ok(cls, v: str) -> str:
        _dotted(v, "detail.parts.path")
        return v


class Detail(_Model):
    """Each posting's own request, for the description the list does not carry: an address with `{ext_id}` in it, and
    where the description is in the answer (JSON: a dotted path; HTML: the page's schema.org JobPosting)."""

    url: str
    method: Literal["GET"] = "GET"
    answer: Literal["json", "html"] = "json"
    #: JSON answers: the dotted path of the description (markup is made text).
    description: str | None = None
    #: JSON answers whose text is split across fields (description, responsibilities, qualifications): the paths read in
    #: order and joined by a blank line, each with an optional heading line above it. Instead of `description`.
    parts: list[DetailPart] = Field(default_factory=list, max_length=PARTS_MAX)
    transforms: list[recipe_ops.Transform] = Field(default_factory=list, max_length=6)

    @field_validator("url")
    @classmethod
    def _public_template(cls, v: str) -> str:
        if v.count("{ext_id}") != 1 or _placeholders(v.replace("{ext_id}", "x")):
            raise ValueError("url holds {ext_id} once and no other placeholder")
        host = re.split(r"[/?#]", v.split("://", 1)[-1], maxsplit=1)[0]
        if "{" in host:
            raise ValueError("a placeholder may not be in the host")
        _need_public(v.replace("{ext_id}", "x"), "detail.url")
        return v

    @model_validator(mode="after")
    def _where(self):
        if self.answer == "json":
            if (self.description is None) == (not self.parts):
                raise ValueError("a JSON detail needs description (the path of the description in the answer) or "
                                 "parts (several paths joined in order), not both")
            if self.description is not None:
                _dotted(self.description, "detail.description")
        elif self.description is not None or self.parts:
            raise ValueError("an HTML detail reads the page's JobPosting: it takes no description path")
        return self


class Limits(_Model):
    """What one night's run of the recipe may spend, over and above the fetcher's own pace and the night's time."""

    requests: int | None = Field(default=None, ge=1, le=REQUESTS_MAX)


class Provenance(_Model):
    """Where a recipe came from: when its traffic was recorded, the hash of that
    recording, who wrote it and with which instructions, and how many recorded postings it reproduced."""

    recorded_at: str = Field(max_length=40)
    recording_sha256: str = Field(max_length=64)
    model: str | None = Field(default=None, max_length=80)
    prompt_version: str | None = Field(default=None, max_length=40)
    tested_on: int | None = Field(default=None, ge=0)


class RequestRecipe(_Model):
    """A data request replayed (`request`), or, with `kind` `embedded`, the page itself read for the results data it
    holds as JSON (`blob`, named as `sites/embedded.py` names a block): the same paging, items and fields, and the
    answer is the page's HTML, not JSON."""

    kind: Literal["request", "embedded"]
    method: Literal["GET", "POST"]
    url: str
    body: dict[str, Any] | None = None
    #: Query parameters sent with every request; a value may hold the placeholders (`PLACEHOLDERS`).
    params: dict[str, str | int | bool] | None = None
    #: Dotted path to the list in the JSON answer ("" when the answer is the list).
    items: str
    #: A dotted path, in one item, for each posting field. `title` and `url` are needed.
    fields: dict[str, str]
    #: Steps applied to a field's value, in order, from the closed list in `recipe_ops.py`.
    transforms: dict[str, list[recipe_ops.Transform]] | None = None
    url_prefix: str | None = None
    page: Paging | None = None
    detail: Detail | None = None
    limits: Limits | None = None
    provenance: Provenance | None = None
    #: `embedded` only: where in the page the results data is (`script:<id>`, `ldjson:<n>` or `var:<name>`).
    blob: str | None = None

    @field_validator("url")
    @classmethod
    def _public(cls, v: str) -> str:
        _need_public(_PLACEHOLDER.sub("0", v), "url")
        return v

    @field_validator("url_prefix")
    @classmethod
    def _prefix_public(cls, v: str | None) -> str | None:
        return None if v is None else _need_public(v, "url_prefix")

    @field_validator("items")
    @classmethod
    def _items_path(cls, v: str) -> str:
        return _dotted(v, "items", allow_empty=True)

    @field_validator("params")
    @classmethod
    def _params_plain(cls, v: dict | None) -> dict | None:
        if v is None:
            return None
        if len(v) > PARAMS_MAX:
            raise ValueError(f"params has more than {PARAMS_MAX} entries")
        for name, value in v.items():
            if not _PARAM.fullmatch(name):
                raise ValueError("a parameter name is plain letters, digits, _ . - [ ]")
            if isinstance(value, str) and (find_secret(value) or len(value) > 200):
                raise ValueError(f"params.{name} carries a token or key, or is too long")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        used = _placeholders(self.url) | _placeholders(self.params) | _placeholders(self.body)
        if unknown := used - set(PLACEHOLDERS):
            raise ValueError(f"{', '.join(sorted(unknown))} is not a placeholder (use "
                             f"{', '.join('{' + n + '}' for n in PLACEHOLDERS)})")
        if used and self.page is None:
            raise ValueError("placeholders go with paging: a recipe with no page has no number to put in them")
        if self.page is not None and used and self.page.param not in used:
            raise ValueError(f"the paging is by {self.page.param}: use {{{self.page.param}}} where it goes")
        host = re.split(r"[/?#]", self.url.split("://", 1)[-1], maxsplit=1)[0]
        if "{" in host:
            raise ValueError("a placeholder may not be in the host")
        for name in (self.transforms or {}):
            if name not in self.fields:
                raise ValueError(f"transforms.{name} is for a field the recipe does not read (fields has no {name})")
        for name, path in self.fields.items():
            if name not in FIELDS:
                raise ValueError(f"fields.{name} is not a posting field (use {', '.join(FIELDS)})")
            _dotted(path, f"fields.{name}")
        for needed in ("title", "url"):
            if needed not in self.fields:
                raise ValueError(f"fields needs {needed}")
        if self.method == "GET" and self.body is not None:
            raise ValueError("a GET request has no body")
        if self.page and self.page.where == "body" and self.method != "POST":
            raise ValueError("page.in is body, but the request is not a POST")
        if len(str(self.body or "")) > 4000:
            raise ValueError("body is too big")
        if self.kind == "embedded":
            if self.blob is None or not embedded.BLOB_NAME.fullmatch(self.blob):
                raise ValueError("blob names where the data is in the page: script:<id>, ldjson:<n> or var:<name>")
            if self.method != "GET" or self.body is not None or self.detail is not None:
                raise ValueError("an embedded recipe reads the page: a GET with no body and no detail")
            if self.page and self.page.param == "cursor":
                raise ValueError("an embedded recipe pages by page or offset, never a cursor")
        elif self.blob is not None:
            raise ValueError("blob is for an embedded recipe")
        return self


class ListPageRecipe(_Model):
    kind: Literal["list_page"]
    url: str
    link_pattern: str
    title_from: Literal["link_text", "title_attr"] = "link_text"
    #: Matches the text or address of the link to the next page; pages are followed until there is none.
    next_pattern: str | None = None

    @field_validator("url")
    @classmethod
    def _public(cls, v: str) -> str:
        return _need_public(v, "url")

    @field_validator("link_pattern")
    @classmethod
    def _link_ok(cls, v: str) -> str:
        compile_pattern(v, "link_pattern")
        return v

    @field_validator("next_pattern")
    @classmethod
    def _next_ok(cls, v: str | None) -> str | None:
        if v is not None:
            compile_pattern(v, "next_pattern")
        return v


class PostingRecipe(_Model):
    """How one employer host's single-posting pages are read (the owner, 2026-10-22): not a listing and not tied to a
    site row. `host` is the host, or `*.base.domain` for a family of hosts that share a system; `url_pattern` matches a
    posting's page address whole and names the posting's id in a group `(?P<ext_id>...)`; `detail` is the request that
    answers with the posting's whole description, as a listing recipe's detail request is."""

    kind: Literal["posting"]
    host: str = Field(max_length=120)
    url_pattern: str
    detail: Detail
    provenance: Provenance | None = None

    @field_validator("host")
    @classmethod
    def _host_ok(cls, v: str) -> str:
        if not re.fullmatch(r"(\*\.)?[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", v.strip().lower()):
            raise ValueError("host is a host name (careers.example.com) or *.example.com, no scheme or path")
        return v.strip().lower()

    @field_validator("url_pattern")
    @classmethod
    def _pattern_ok(cls, v: str) -> str:
        compile_pattern(v, "url_pattern")
        if "(?P<ext_id>" not in v:
            raise ValueError("url_pattern names the posting's id in a group: (?P<ext_id>...)")
        return v


#: A CSS selector a page recipe may hold: the characters a selector is made of, never a brace, a semicolon, a backslash,
#: an angle bracket or a comment end, and not so long that it is a program.
SELECTOR_MAX = 200
_SELECTOR_CHARS = re.compile(r"^[\w\s.#\[\]=\"'^$*~|>+,:()/%@!&-]+$")


def css(v: str, what: str) -> str:
    """A selector as kept, or ValueError saying why not."""
    v = (v or "").strip() if isinstance(v, str) else ""
    if not v or len(v) > SELECTOR_MAX:
        raise ValueError(f"{what} must be a CSS selector of 1 to {SELECTOR_MAX} characters")
    if not _SELECTOR_CHARS.match(v) or "*/" in v or "javascript:" in v.lower():
        raise ValueError(f"{what} may hold only the characters of a plain CSS selector")
    return v


class PageRecipe(_Model):
    """A site read in the app's own Chrome by recorded steps (`jobsearch/browser/recipes/page_run.py`): `url` is the
    page the steps
    start from; `search` the search box the owner's terms are typed in (none: the page is read as it loads) and `submit`
    the control that runs it (none: the box's own form, or Enter); `links` the anchors of the postings, `row` the
    element that holds one posting's anchor and `location` the place inside it; `next` the control that shows the next
    page of results; `text` the element of a posting's page that holds its description."""

    kind: Literal["page"]
    url: str
    search: str | None = None
    submit: str | None = None
    links: str
    row: str | None = None
    location: str | None = None
    next: str | None = None
    text: str | None = None
    provenance: Provenance | None = None

    @field_validator("url")
    @classmethod
    def _public(cls, v: str) -> str:
        return _need_public(v, "url")

    @field_validator("search", "submit", "links", "row", "location", "next", "text", mode="before")
    @classmethod
    def _selector(cls, v: Any, info) -> Any:
        return None if v is None else css(v, info.field_name)

    @model_validator(mode="after")
    def _needs(self) -> "PageRecipe":
        if self.submit and not self.search:
            raise ValueError("submit goes with a search box")
        if self.location and not self.row:
            raise ValueError("location is read inside a row")
        return self


class HtmlRecipe(_Model):
    """A site whose postings are in the page's own HTML (docs/PRD.md path 49): read by code through the polite fetcher,
    with selectors recorded from the rendered page (`jobsearch/browser/recipes/html_run.py`). `url` is the start page;
    `links` the
    anchors of the postings (the title is the link's own words), `row` the element that holds one posting and, inside
    it, `location` and `date`; `next` the anchor of the next page of results (followed by its address, never clicked);
    `text` the element of a posting's page that holds its description. All of them are selectors of the closed grammar
    in `html_select.py`."""

    kind: Literal["html"]
    url: str
    links: str
    row: str | None = None
    location: str | None = None
    date: str | None = None
    next: str | None = None
    text: str | None = None
    provenance: Provenance | None = None

    @field_validator("url")
    @classmethod
    def _public(cls, v: str) -> str:
        return _need_public(v, "url")

    @field_validator("links", "row", "location", "date", "next", "text", mode="before")
    @classmethod
    def _selector(cls, v: Any, info) -> Any:
        if v is None:
            return None
        try:
            return html_select.check(css(v, info.field_name))
        except ValueError as exc:
            raise ValueError(f"{info.field_name}: {exc}") from None

    @model_validator(mode="after")
    def _needs(self) -> "HtmlRecipe":
        if (self.location or self.date) and not self.row:
            raise ValueError("location and date are read inside a row")
        return self


class HtmlPostingRecipe(_Model):
    """How one employer host's single-posting pages are read from their own HTML (docs/PRD.md path 49): `host` is the
    host or `*.base.domain`, `url_pattern` matches a posting page's address whole, and the text is either `text`, the
    selector of the element that holds the description, or `jsonld: true`, the description of the schema.org JobPosting
    the page embeds in a `ld+json` script (for a page whose HTML holds no text of its own: `html_recipe.jsonld_text`).
    Exactly one of the two. Not tied to a site row, as a `posting` recipe is not."""

    kind: Literal["html_posting"]
    host: str = Field(max_length=120)
    url_pattern: str
    text: str | None = None
    jsonld: bool = False
    provenance: Provenance | None = None

    @field_validator("host")
    @classmethod
    def _host_ok(cls, v: str) -> str:
        if not re.fullmatch(r"(\*\.)?[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", v.strip().lower()):
            raise ValueError("host is a host name (careers.example.com) or *.example.com, no scheme or path")
        return v.strip().lower()

    @field_validator("url_pattern")
    @classmethod
    def _pattern_ok(cls, v: str) -> str:
        compile_pattern(v, "url_pattern")
        return v

    @field_validator("text", mode="before")
    @classmethod
    def _selector(cls, v: Any) -> Any:
        if v is None:
            return None
        try:
            return html_select.check(css(v, "text"))
        except ValueError as exc:
            raise ValueError(f"text: {exc}") from None

    @model_validator(mode="after")
    def _one_source(self) -> "HtmlPostingRecipe":
        if (self.text is None) == (not self.jsonld):
            raise ValueError("a posting's text is either `text` (a selector) or `jsonld: true`, not both or neither")
        return self


def host_covers(pattern: str, host: str) -> bool:
    """Whether a posting recipe's `host` (a host, or `*.base`) covers this host (`www.` and case left out, on both
    sides: a recipe recorded for `www.example.com` covers `example.com` and the other way round, because the app keeps
    every address's host without its `www.`)."""
    base = pattern.strip().lower().removeprefix("*.").removeprefix("www.")
    bare = (host or "").strip().lower().removeprefix("www.")
    return bare == base or (pattern.startswith("*.") and bare.endswith("." + base))


#: What a results row can give (docs/PRD.md 37.1); `title` is the one that is needed.
BROWSER_FIELDS = ("title", "company", "location", "posted", "pay", "work_mode", "snippet", "apply_url")


def _browser_text(v: str) -> str:
    if not v.strip():
        raise ValueError("is empty")
    if len(v) > BROWSER_TEXT_MAX:
        raise ValueError(f"is longer than {BROWSER_TEXT_MAX} characters")
    return v


#: A line of plain words a reader acts on ("the Search jobs box at the top").
BrowserText = Annotated[str, AfterValidator(_browser_text)]


def _browser_address(v: str, what: str, placeholders: tuple[str, ...] = ()) -> str:
    """A public address with no token or key in it. `placeholders` ({phrase}) are filled in before the check."""
    filled = v
    host = re.split(r"[/?#]", v.split("://", 1)[-1], maxsplit=1)[0]
    for name in placeholders:
        if name in host:
            raise ValueError("a placeholder may not be in the host")
        filled = filled.replace(name, "x")
    _need_public(filled, what)
    if reason := addresses.address_secret(filled):
        raise ValueError(f"{what} carries a token or key ({reason})")
    return v


class BrowserRecency(_Model):
    #: The menu or filter that limits how old a posting is, and its options as the site writes them, newest first.
    control: BrowserText
    choices: list[BrowserText]


class BrowserSearch(_Model):
    #: `boxes` types into the site's own boxes, `url` is an address with the words in it, `none` has no search.
    how: Literal["boxes", "url", "none"]
    phrase_box: BrowserText | None = None
    place_box: BrowserText | None = None
    url_template: str | None = None
    recency: BrowserRecency | None = None
    newest: BrowserText | None = None

    @field_validator("url_template")
    @classmethod
    def _template_public(cls, v: str | None) -> str | None:
        return None if v is None else _browser_address(v, "url_template", ("{phrase}", "{place}"))

    @model_validator(mode="after")
    def _complete(self):
        if self.how == "boxes" and self.phrase_box is None:
            raise ValueError("phrase_box is needed when the search types into boxes")
        if self.how == "url":
            if self.url_template is None:
                raise ValueError("url_template is needed when the search is an address")
            if "{phrase}" not in self.url_template:
                raise ValueError("url_template must hold {phrase}, where the search words go")
        return self


class BrowserResults(_Model):
    #: What one listing looks like, where each field is read from a row, and how the next page loads ("none": no more).
    row: BrowserText
    fields: dict[str, BrowserText]
    next: BrowserText

    @model_validator(mode="after")
    def _consistent(self):
        for name in self.fields:
            if name not in BROWSER_FIELDS:
                raise ValueError(f"fields.{name} is not a result field (use {', '.join(BROWSER_FIELDS)})")
        if "title" not in self.fields:
            raise ValueError("fields needs title")
        return self


class BrowserPosting(_Model):
    #: Where the whole text is on a posting page, and where its apply link is.
    description: BrowserText
    apply: BrowserText | None = None


class BrowserBookmark(_Model):
    """One search worked out for the site: an address that carries the criteria, for a phrase and a place."""

    phrase: BrowserText
    place: BrowserText
    url: str

    @field_validator("url")
    @classmethod
    def _public(cls, v: str) -> str:
        return _browser_address(v, "url")


class BrowserRecipe(_Model):
    """How the owner's browser session reads a site (docs/PRD.md 37.1). Words, not code: the server never runs it.

    Two shapes. The Pathfinder's runbook shape holds the site's `runbook` (markdown, in the shape of
    browser.pathfinder.runbook.template.md) and the few machine fields the reader and the app need: `bookmarks`,
    `ats` (the job system's name) and `new_since` (one line). The older shape (`search`, `results`, `posting`, ...)
    is still read, so a site not yet re-mapped keeps working."""

    kind: Literal["browser"]
    start_url: str
    search: BrowserSearch | None = None
    results: BrowserResults | None = None
    posting: BrowserPosting | None = None
    recommended_url: str | None = None
    notes: list[BrowserText] = Field(default_factory=list)
    runbook: str | None = None
    bookmarks: list[BrowserBookmark] = Field(default_factory=list)
    ats: BrowserText | None = None
    new_since: BrowserText | None = None

    @field_validator("runbook")
    @classmethod
    def _runbook_fits(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.strip():
            raise ValueError("is empty")
        if len(v) > BROWSER_RUNBOOK_MAX:
            raise ValueError(f"is longer than {BROWSER_RUNBOOK_MAX} characters ({len(v)}): shorten it, move detail "
                             "out, and send it again")
        if reason := find_secret(v):
            raise ValueError(f"carries a token or key ({reason})")
        return v

    @field_validator("bookmarks")
    @classmethod
    def _few_bookmarks(cls, v: list[BrowserBookmark]) -> list[BrowserBookmark]:
        if len(v) > BROWSER_BOOKMARKS_MAX:
            raise ValueError(f"has more than {BROWSER_BOOKMARKS_MAX} bookmarks")
        return v

    @field_validator("start_url")
    @classmethod
    def _start_public(cls, v: str) -> str:
        return _browser_address(v, "start_url")

    @field_validator("recommended_url")
    @classmethod
    def _recommended_public(cls, v: str | None) -> str | None:
        return None if v is None else _browser_address(v, "recommended_url")

    @field_validator("notes")
    @classmethod
    def _few_notes(cls, v: list[str]) -> list[str]:
        if len(v) > BROWSER_NOTES_MAX:
            raise ValueError(f"has more than {BROWSER_NOTES_MAX} notes")
        return v

    @model_validator(mode="after")
    def _short_enough(self):
        if self.runbook is None:
            for name in ("search", "results", "posting"):
                if getattr(self, name) is None:
                    raise ValueError(f"{name}: is missing (or send a runbook)")
            if self.bookmarks or self.ats or self.new_since:
                raise ValueError("bookmarks, ats and new_since go with a runbook")
            dumped = self.model_dump(mode="json", by_alias=True, exclude=set(BROWSER_RUNBOOK_FIELDS))
            if len(json.dumps(dumped)) > BROWSER_RECIPE_MAX:
                raise ValueError(f"the recipe is longer than {BROWSER_RECIPE_MAX} characters")
        elif len(json.dumps(self.model_dump(mode="json", by_alias=True))) > BROWSER_RUNBOOK_RECIPE_MAX:
            raise ValueError(f"the recipe is longer than {BROWSER_RUNBOOK_RECIPE_MAX} characters")
        return self


Recipe = Annotated[
    AdapterRecipe | RequestRecipe | ListPageRecipe | BrowserRecipe | PostingRecipe | PageRecipe | HtmlRecipe
    | HtmlPostingRecipe,
    Field(discriminator="kind")]
_RECIPE = TypeAdapter(Recipe)


def _where(err: dict) -> str:
    loc = [str(p) for p in err.get("loc", ()) if not str(p).startswith(("function-after", "function-before"))]
    # The union tag is not a part of the recipe a person wrote.
    if loc and loc[0] in KINDS:  # only the leading one: a browser recipe has a field `posting`, which is not a tag
        loc = loc[1:]
    return ".".join(loc)


def _problem(err: dict, kind: str) -> str:
    msg = str(err.get("msg", "")).removeprefix("Value error, ")
    if err.get("type") == "extra_forbidden":
        msg = f"is not part of the {kind} recipe"
    elif err.get("type") == "missing":
        msg = "is missing"
    where = _where(err)
    return f"{where}: {msg}" if where else msg


def _shaped(raw: Any) -> None:
    """ValueError when `raw` is not even the shape of a recipe (an object of a known kind, not too big)."""
    if not isinstance(raw, dict):
        raise ValueError("a recipe is a JSON object")
    if raw.get("kind") not in KINDS:
        raise ValueError(f"unknown recipe kind {raw.get('kind')!r} (use {', '.join(KINDS)})")
    if len(str(raw)) > (2 * BROWSER_RUNBOOK_RECIPE_MAX if raw.get("kind") == "browser" else RECIPE_MAX_BYTES):
        raise ValueError("the recipe is too big")


def parse_recipe(
    raw: Any,
) -> (AdapterRecipe | RequestRecipe | ListPageRecipe | BrowserRecipe | PostingRecipe | PageRecipe | HtmlRecipe
      | HtmlPostingRecipe):
    """The recipe, checked. ValueError names the problem in words ("fields needs title"); nothing else is let in."""
    _shaped(raw)
    try:
        return _RECIPE.validate_python(raw)
    except ValidationError as e:
        raise ValueError(_problem(e.errors()[0], raw["kind"])) from None


def parse_page(raw: Any) -> PageRecipe:
    """A page recipe, checked; ValueError when the recipe is of another kind."""
    got = parse_recipe(raw)
    if not isinstance(got, PageRecipe):
        raise ValueError("this is not a page recipe")
    return got


def recipe_problems(raw: Any) -> list[str]:
    """Every problem with a recipe, each in words (`parse_recipe` names only the first); empty when it is valid."""
    try:
        _shaped(raw)
    except ValueError as exc:
        return [str(exc)]
    try:
        _RECIPE.validate_python(raw)
    except ValidationError as e:
        return [_problem(err, raw["kind"]) for err in e.errors()]
    return []


# --- running a recipe ------------------------------------------------------------------------------------------------


def _dig(value: Any, path: str) -> Any:
    """What a dotted path points at in parsed JSON (a number is a list position); None when it is not there."""
    if path in ("", "."):
        return value
    for seg in path.split("."):
        if isinstance(value, dict):
            value = value.get(seg)
        elif isinstance(value, list) and seg.isdigit() and int(seg) < len(value):
            value = value[int(seg)]
        else:
            return None
    return value


def _words(v: Any) -> str | None:
    """A field's value as one line of text: a string, a number, a list of those, or a {name|label|text|title}."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    if isinstance(v, str):
        return " ".join(v.split()) or None
    if isinstance(v, list):
        return ", ".join(w for w in (_words(x) for x in v) if w) or None
    if isinstance(v, dict):
        for key in ("name", "label", "text", "title", "value"):
            if key in v:
                return _words(v[key])
    return None


def _link(value: str | None, base: str) -> str | None:
    """A posting's address, whole: joined to `base` when relative. None for anything that is not http(s)."""
    if not value:
        return None
    joined = urljoin(base, value.strip())
    return joined if urlparse(joined).scheme in ("http", "https") and len(joined) <= 1000 else None


def _with_query(url: str, extra: dict[str, Any]) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in extra]
    query += [(k, str(v)) for k, v in extra.items()]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _row_posting(row: dict, fields: dict[str, str], base: str,
                 transforms: dict[str, list[recipe_ops.Transform]] | None = None) -> dict:
    budget = _Budget()
    got: dict[str, Any] = {}
    for name, path in fields.items():
        value = _dig(row, path)
        steps = (transforms or {}).get(name)
        got[name] = recipe_ops.apply(steps, value, budget) if steps else value
    location = _words(got.get("location"))
    summary = _words(got.get("summary")) if not isinstance(got.get("summary"), str) else got["summary"]
    comp_text = _words(got.get("comp"))
    low, high = parse_comp(comp_text)
    url = _link(_words(got.get("url")), base)
    title = _words(got.get("title"))
    posting = _posting(
        raw=row, ext_id=_words(got.get("ext_id")) or url or title, title=title, location=location,
        work_mode=_mode(None, location), comp=comp_text, comp_min=low, comp_max=high,
        posted=_loose_date(got.get("posted")), url=url, apply_url=url,
        text=html_to_text(summary) or None, department=_words(got.get("department")))
    if "company" in fields:  # only a recipe that maps it carries one: the night's read keeps it (site_reads.py)
        posting["company"] = _words(got.get("company"))
    return posting


def _filled(value: Any, numbers: dict[str, Any]) -> Any:
    """`value` with each placeholder replaced by the paging's number: a text that is only a placeholder becomes the
    number itself, and an entry whose value is `{cursor}` is left out on the first page (there is no cursor yet)."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole and whole.group(1) in numbers and numbers[whole.group(1)] is not None:
            return numbers[whole.group(1)]
        return _PLACEHOLDER.sub(
            lambda m: "" if numbers.get(m.group(1)) is None else str(numbers[m.group(1)]), value)
    if isinstance(value, dict):
        return {k: _filled(v, numbers) for k, v in value.items() if not (v == "{cursor}" and not numbers.get("cursor"))}
    if isinstance(value, list):
        return [_filled(v, numbers) for v in value]
    return value


def _request_of(r: RequestRecipe, index: int, cursor: str | None) -> tuple[str, dict | None]:
    """The address and body of the request for page `index` (0 is the first). A recipe that names its placeholders
    gets them filled in; one that does not has the paging's number put under the paging's own name, as before."""
    size = r.page.size if r.page else None
    numbers = {"page": index + 1, "offset": index * (size or 0), "cursor": cursor, "size": size}
    named = r.page is not None and bool(_placeholders(r.url) | _placeholders(r.params) | _placeholders(r.body))
    url = _filled(r.url, numbers) if named else r.url
    body = _filled(r.body, numbers) if named else (dict(r.body) if r.body is not None else None)
    extra = {k: ("true" if v is True else "false" if v is False else v)
             for k, v in (_filled(r.params, numbers) if r.params else {}).items()}
    if r.page and not named:
        p = r.page
        key = p.name or ("cursor" if p.param == "cursor" else p.param)
        number = cursor if p.param == "cursor" else index * p.size if p.param == "offset" else index + 1
        sent = {**({key: number} if number is not None and (p.param != "cursor" or number) else {}),
                **({p.size_name: p.size} if p.size_name else {})}
        if p.where == "body":
            body = {**(body or {}), **sent}
        else:
            extra = {**extra, **sent}
    if extra:
        url = _with_query(url, extra)
    return url, body


async def _run_request(fetch: Fetcher, r: RequestRecipe, keep_bad: bool, enough: int | None = None) -> Listing:
    """The data request, replayed: once, or a page at a time to the site's last page (a short page, an empty one, no
    cursor). A full page that brings nothing not already read means the site ignores the page number, the time running
    out ends it too, and so does the recipe's own request limit; each is said, and the listing is truncated."""
    base = r.url_prefix or r.url
    postings: list[dict] = []
    seen: set[str] = set()
    stopped: str | None = None
    dropped = 0
    index = 0
    requests = 0
    cursor: str | None = None
    allowed = r.limits.requests if r.limits else None
    while True:
        url, body = _request_of(r, index, cursor)
        sent_body = {"json": body} if r.method == "POST" and body is not None else {}
        reply = await fetch.request(r.method, url, **sent_body)
        requests += 1
        host = urlparse(url).hostname
        if reply.status_code != 200:
            raise SkillError(f"{host} answered {reply.status_code}", reply.status_code)
        if r.kind == "embedded":
            answer = embedded.extract(reply.text, r.blob or "")
            if answer is None:
                raise SkillError(f"{host}'s page no longer holds the data block {r.blob}: the site has changed shape")
        else:
            try:
                answer = reply.json()
            except ValueError:
                raise SkillError(f"{host} did not answer with JSON") from None
        rows = _dig(answer, r.items)
        if not isinstance(rows, list):
            raise SkillError(f"no list found at {r.items!r} in the answer: the site has changed shape")
        rows = [x for x in rows if isinstance(x, dict)]
        fresh = 0
        for row in rows:
            posting = _row_posting(row, r.fields, base, r.transforms)
            key = posting["ext_id"] or repr(sorted(row.items(), key=lambda kv: kv[0]))[:200]
            if key in seen:
                continue
            seen.add(key)
            fresh += 1
            if not posting["title"] and not keep_bad:
                dropped += 1
                continue
            postings.append(posting)
        following = None
        if r.page and r.page.param == "cursor" and r.page.cursor_path:
            following = _words(_dig(answer, r.page.cursor_path))
        short = bool(r.page) and r.page.param != "cursor" and len(rows) < r.page.size
        if not r.page or not rows or short or (r.page.param == "cursor" and not following):
            break  # the only page, an empty one, a short one or the cursor's last: the site's end
        if not fresh or (following is not None and following == cursor):
            stopped = PAGE_REPEATED  # a full page of postings already read: the site ignores the page number
            break
        if timeup(fetch):
            stopped = TIME_RAN_OUT
            break
        if enough is not None and len(postings) >= enough:
            stopped = ENOUGH_READ
            break
        if allowed is not None and requests >= allowed:
            stopped = LIMIT_REACHED
            break
        cursor = following
        index += 1
    notes = [stopped_at(len(postings), stopped)] if stopped else []
    if dropped:
        notes.append(f"{dropped} rows had no title and were left out")
    return Listing(postings, stopped is not None, "; ".join(notes) or None)


@dataclass
class DetailRead:
    """One posting's detail request, answered: its description (None when the answer holds none, or the posting is gone)
    and the reply, which the raw copy keeps."""

    ext_id: str
    text: str | None
    reply: Any
    url: str


def detail_url(r: RequestRecipe | PostingRecipe, ext_id: str) -> str:
    assert r.detail is not None
    return r.detail.url.replace("{ext_id}", quote(str(ext_id), safe=""))


async def read_detail(fetch: Fetcher, r: RequestRecipe | PostingRecipe, ext_id: str) -> DetailRead:
    """The detail request of a recipe for one posting (`detail`): the description in the answer, as plain text. A 404 or
    410 is a posting that is gone (no text); any other answer but 200, or one that is not what the recipe reads, is a
    SkillError. Raises ValueError for a recipe with no detail."""
    if r.detail is None:
        raise ValueError("this recipe has no detail request")
    d = r.detail
    url = detail_url(r, ext_id)
    reply = await fetch.request("GET", url)
    host = urlparse(url).hostname
    if polite.gone(url, reply.status_code) is not None:
        return DetailRead(ext_id, None, reply, url)
    if reply.status_code != 200:
        raise SkillError(f"{host} answered {reply.status_code}", reply.status_code)
    if d.answer == "html":
        return DetailRead(ext_id, (page_facts(reply.text).get("text") or None), reply, url)
    try:
        answer = reply.json()
    except ValueError:
        raise SkillError(f"{host} did not answer with JSON") from None
    if d.parts:  # the text is split across fields: each read the same way, in order, headings above
        blocks = []
        for part in d.parts:
            value = recipe_ops.apply(d.transforms, _dig(answer, part.path), _Budget())
            words = html_to_text(value if isinstance(value, str) else (_words(value) or ""))
            if words:
                blocks.append(f"{part.heading}\n{words}" if part.heading else words)
        return DetailRead(ext_id, "\n\n".join(blocks) or None, reply, url)
    assert d.description is not None
    value = recipe_ops.apply(d.transforms, _dig(answer, d.description), _Budget())
    text = html_to_text(value if isinstance(value, str) else (_words(value) or ""))
    return DetailRead(ext_id, text or None, reply, url)


async def read_posting(fetch: Fetcher, r: PostingRecipe, link: str) -> DetailRead | None:
    """One posting's text by a host's posting recipe: the id is taken from the page address `link` (None when the
    address is not one the recipe's pattern matches whole), then the recipe's detail request is made, as for a listing
    recipe's detail (`read_detail`)."""
    match = compile_pattern(r.url_pattern, "url_pattern").fullmatch(link.split("#", 1)[0])
    if match is None or not match.group("ext_id"):
        return None
    return await read_detail(fetch, r, match.group("ext_id"))


class _Anchors(HTMLParser):
    """The links of a page: (address, the words inside, the title attribute)."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.found: list[tuple[str, str, str]] = []
        self._open: tuple[str, str] | None = None
        self._words: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        self.handle_endtag("a")
        attrs = dict(attrs)
        href = _link(attrs.get("href"), self.base)
        if href:
            self._open, self._words = (href, (attrs.get("title") or "").strip()), []

    def handle_data(self, data):
        if self._open:
            self._words.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            self.found.append((self._open[0], " ".join(" ".join(self._words).split()), self._open[1]))
            self._open = None


def _anchors(page: str, base: str) -> list[tuple[str, str, str]]:
    parser = _Anchors(base)
    parser.feed(page[:HTML_MAX])
    parser.close()
    return parser.found


def _same_host(a: str, b: str) -> bool:
    return (urlparse(a).hostname or "") == (urlparse(b).hostname or "")


async def _run_list_page(fetch: Fetcher, r: ListPageRecipe, keep_bad: bool, enough: int | None = None) -> Listing:
    """The list pages, read from the first and on through each `next_pattern` link to the last. A page that brings no
    link not already read ends it: with no link at all it is the end, with links all read before the pages are going
    round in circles, and the time running out ends it too; both are said, and the listing is truncated."""
    link_re = compile_pattern(r.link_pattern, "link_pattern")
    next_re = compile_pattern(r.next_pattern, "next_pattern") if r.next_pattern else None
    budget = _Budget()
    found: dict[str, dict] = {}
    visited: set[str] = set()
    page_url: str | None = r.url
    stopped: str | None = None
    while True:
        assert page_url
        visited.add(page_url)
        reply = await fetch.get(page_url)
        if reply.status_code != 200:
            raise SkillError(f"{urlparse(page_url).hostname} answered {reply.status_code}", reply.status_code)
        anchors = _anchors(reply.text, page_url)
        before, matched = len(found), 0
        for href, words, title_attr in anchors:
            if not budget.search(link_re, href):
                continue
            matched += 1
            key = href.split("#", 1)[0]
            title = (words if r.title_from == "link_text" else title_attr)[:300] or None
            if key in found:
                found[key]["title"] = found[key]["title"] or title
                continue
            found[key] = {"title": title, "url": key}
        following = None
        if next_re:
            for href, words, _ in anchors:
                if href not in visited and _same_host(href, r.url) and public_url(href) \
                        and (budget.search(next_re, href) or budget.search(next_re, words)):
                    following = href
                    break
        if not following:
            break
        if len(found) == before:  # no link not read already: past the last page, or going round in circles
            stopped = PAGE_REPEATED if matched else None
            break
        if timeup(fetch):
            stopped = TIME_RAN_OUT
            break
        if enough is not None and len(found) >= enough:
            stopped = ENOUGH_READ
            break
        page_url = following
    postings: list[dict] = [
        _posting(raw=row, ext_id=row["url"], title=row["title"], url=row["url"], apply_url=row["url"])
        for row in found.values()]
    dropped = sum(1 for p in postings if not p["title"])
    if not keep_bad:
        postings = [p for p in postings if p["title"]]
    notes = [stopped_at(len(found), stopped)] if stopped else []
    if dropped and not keep_bad:
        notes.append(f"{dropped} links had no title")
    return Listing(postings, stopped is not None, "; ".join(notes) or None)


def reparse_item(recipe: Any, raw: Any) -> dict | None:
    """The plain posting this recipe makes of one raw item (a row of a data request, a link of a list page, an adapter
    recipe's list item), with no fetch; None when the recipe leaves it out or has no list item to read. Raises
    ValueError for a recipe that is not valid."""
    r = recipe if isinstance(recipe, BaseModel) else parse_recipe(recipe)
    if isinstance(r, AdapterRecipe):
        return ADAPTERS[r.ats].parse_item(raw, dict(r.board))
    if isinstance(r, RequestRecipe) and isinstance(raw, dict):
        posting = _row_posting(raw, r.fields, r.url_prefix or r.url, r.transforms)
        return posting if posting["title"] else None
    if isinstance(r, ListPageRecipe) and isinstance(raw, dict) and raw.get("title"):
        return _posting(raw=raw, ext_id=raw["url"], title=raw["title"], url=raw["url"], apply_url=raw["url"])
    return None


async def run_recipe(fetch: Fetcher, recipe: Any, *, keep_bad: bool = False, enough: int | None = None) -> Listing:
    """Read a site through its recipe (a dict, or a parsed `Recipe`) and hand back the postings in the shape every job
    system gives. Raises ValueError for a recipe that is not valid, SkillError when the site no longer answers the way
    the recipe reads it, and `polite.Refused` when the site asks not to be read (the caller decides what that means).

    A posting always has a title, unless `keep_bad` (the Check's read, which counts what is missing). With `enough`, a
    list read stops after the page that brings that many postings (a test of a recipe needs no more)."""
    r = recipe if isinstance(recipe, BaseModel) else parse_recipe(recipe)
    if isinstance(r, AdapterRecipe):
        try:
            return await ADAPTERS[r.ats].read(fetch, dict(r.board))
        except AdapterError as e:
            raise SkillError(str(e)) from e
    if isinstance(r, RequestRecipe):
        return await _run_request(fetch, r, keep_bad, enough)
    if isinstance(r, ListPageRecipe):
        return await _run_list_page(fetch, r, keep_bad, enough)
    if isinstance(r, BrowserRecipe):
        raise SkillError("a browser skill is the browser's to run: the night's read leaves this site alone")
    if isinstance(r, PostingRecipe):
        raise SkillError("a posting recipe reads one posting's page, not a site's listing")
    if isinstance(r, PageRecipe):
        raise SkillError("a page recipe is run in the app's own Chrome (`page_recipe.run`), not through the fetcher")
    if isinstance(r, HtmlPostingRecipe):
        raise SkillError("an html posting recipe reads one posting's page, not a site's listing")
    if isinstance(r, HtmlRecipe):
        from app.jobsearch.browser.recipes import html_run as html_recipe  # it reads the whole job-search side

        return await html_recipe.run(fetch, r, enough=enough)
    raise SkillError(f"{type(r).__name__} is not a recipe the app can run")


# --- versions --------------------------------------------------------------------------------------------------------


async def current_skill(session: AsyncSession, site_id: uuid.UUID | None) -> SiteSkill | None:
    """The newest version of a site's skill that is not retired, or None."""
    if site_id is None:
        return None
    return (await session.scalars(
        select(SiteSkill).where(SiteSkill.site_id == site_id, SiteSkill.retired_at.is_(None))
        .order_by(SiteSkill.version.desc()).limit(1)
    )).first()


async def save_skill(
    session: AsyncSession, owner: str, site_id: uuid.UUID, recipe: Any, project_id: uuid.UUID | None = None,
) -> SiteSkill:
    """Check a recipe and keep it as the site's next version. ValueError for a recipe that is not valid or a site that
    is not this owner's. Retires nothing: every earlier version stays, as the history of what was tried. A site that
    is off and that nobody has decided about is switched on by its runbook (`where.runbook_saved`)."""
    from app.sites import where  # where reads this module's skills, so it is imported here

    parsed = parse_recipe(recipe)
    site = await session.get(Site, site_id)
    if site is None or site.owner != owner:
        raise ValueError("no such site")
    last = await session.scalar(select(func.max(SiteSkill.version)).where(SiteSkill.site_id == site_id))
    skill = SiteSkill(
        owner=owner, site_id=site_id, version=(last or 0) + 1, recipe=parsed.model_dump(mode="json", by_alias=True),
        made_by_project_id=project_id, created_at=datetime.now(UTC),
    )
    session.add(skill)
    await session.flush()
    await where.runbook_saved(session, site_id)
    return skill


async def skills_of_site(session: AsyncSession, site_id: uuid.UUID | None) -> list[SiteSkill]:
    """Every version of the site's skill, oldest first."""
    if site_id is None:
        return []
    return list((await session.scalars(
        select(SiteSkill).where(SiteSkill.site_id == site_id).order_by(SiteSkill.version)
    )).all())


async def keep_skill(session: AsyncSession, project, recipe: Any) -> SiteSkill:
    """Act keeps the recipe Do built as the site's next version, made by this project (docs/PRD.md 29.2): checked by
    `parse_recipe` and saved by `save_skill`; the version before it is retired, so the runner reads the new one."""
    from app.sites.base import PlanEngineError  # the loop's error type; imported here to keep skills free of the loop

    if project.site_id is None:
        raise PlanEngineError("this project has no site, so there is no skill to keep")
    try:
        browser = parse_recipe(recipe).kind == "browser"
    except ValueError:
        browser = False  # `save_skill` names what is wrong with it
    if browser:
        raise PlanEngineError(*BROWSER_REFUSAL)
    try:
        skill = await save_skill(session, project.owner, project.site_id, recipe, project.id)
    except ValueError as exc:
        raise PlanEngineError(f"`skill` isn't a recipe the app can run: {exc}") from exc
    for old in (await session.scalars(select(SiteSkill).where(
            SiteSkill.site_id == project.site_id, SiteSkill.retired_at.is_(None), SiteSkill.id != skill.id))).all():
        old.retired_at = skill.created_at
    await session.flush()
    return skill


async def retire_skill(session: AsyncSession, skill: SiteSkill) -> SiteSkill:
    """A version that broke or was replaced: kept, not deleted, and no longer the one the app runs."""
    if skill.retired_at is None:
        skill.retired_at = datetime.now(UTC)
        await session.flush()
    return skill


async def broken_sites(session: AsyncSession, after: dict[uuid.UUID, datetime | None]) -> set[uuid.UUID]:
    """Of the sites in `after` (site id -> the `after` of `broken`), those whose skill broke, in one query. The caller
    has already seen that none has a version in force, which `broken` asks first."""
    if not after:
        return set()
    newest = dict((await session.execute(
        select(SiteSkill.site_id, func.max(SiteSkill.retired_at))
        .where(SiteSkill.site_id.in_(after), SiteSkill.retired_at.is_not(None)).group_by(SiteSkill.site_id))).all())
    return {site_id for site_id, since in after.items()
            if newest.get(site_id) is not None and (since is None or newest[site_id] > since)}


async def broken(session: AsyncSession, site_id: uuid.UUID | None, after: datetime | None = None) -> SiteSkill | None:
    """The version that broke (docs/PRD.md 29.4): the newest retired one, when the site has no version in force, and,
    with `after`, only if it was retired after then (a project's end: what broke before that project is its business,
    not a new reason to start one). None for a site whose skill works, or that never had one."""
    if site_id is None or await current_skill(session, site_id) is not None:
        return None
    query = select(SiteSkill).where(SiteSkill.site_id == site_id, SiteSkill.retired_at.is_not(None))
    if after is not None:
        query = query.where(SiteSkill.retired_at > after)
    return (await session.scalars(query.order_by(SiteSkill.retired_at.desc()).limit(1))).first()
