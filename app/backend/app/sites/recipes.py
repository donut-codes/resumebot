"""Recording a site's job listings and checking a recipe against the recording. Pure of the network: a recording is data
the app's own Chrome made (`jobsearch/browser/recipes/record.py`), and everything here works on it.

**Capture.** What one load of a site's listing page asked for and was answered: for each response the method, address,
body sent, status, content type and body. `sanitize` keeps only what a recipe could replay: GET and POST answered 200
with JSON, on public addresses, at most `RESPONSES_MAX` of them, each at most `BODY_MAX` bytes. Request headers and
cookies are never part of a capture (the browser layer does not pass them on); a response whose address or body sent
carries a token, key or session (`security.find_secret`, or a parameter named like one) is dropped, because a recipe
that needs one is refused anyway. The capture's hash (`fingerprint`) is what a recipe's provenance names.

**What the model sees.** `trim` makes the capture short and safe to read as data: for each response its request and the
shape of the answer, with the longest lists in it and their first items cut to a few hundred characters. Never
instructions: the writer's instructions say so.

**Replay.** `ReplayFetcher` stands where the polite fetcher stands and answers each request from the capture, so the
one executor (`skills.run_recipe`) runs the recipe with no network. A request the capture has no answer for gets the
nearest recorded answer for the same endpoint with its list emptied (the pages the page never loaded: the read then
ends there); an endpoint never recorded is an error. `verify_offline` runs a recipe so and says whether it reproduced
the recorded list: rows found against rows recorded, required fields filled in 90 percent or more, dates that parse,
pay that is plausible, public addresses, and a detail request only if the capture shows one.

**Health.** `judge_night` says whether a night's read looks broken (an error, no rows, rows far under the recent
median, fields no longer filled in). A block is not breakage: it is a stop, and cools the site."""

import hashlib
import json
import re
import statistics
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.agents.security import find_secret
from app.jobsearch.fetch.polite import Fetcher, public_url
from app.jobsearch.postings import whole
from app.sites import embedded, skills

RESPONSES_MAX = 40
#: A job list with every description in it is megabytes (a board's whole list in one answer): each answer may be this
#: big, and a capture this big in all.
BODY_MAX = 6_000_000
CAPTURE_MAX = 16_000_000
CAPTURE_VERSION = 1
#: A parameter or key named like a credential.
_SECRET_NAME = re.compile(
    r"token|secret|session|passw|auth|cookie|csrf|xsrf|jwt|signature|api[-_]?key|bearer|^sig$|^key$", re.I)
#: What the model is given of a capture, in characters, and of each text in an item.
TRIM_MAX = 60_000
#: The most of a request's own body shown to the writer (a search payload is far less; the recording keeps it whole).
POST_SHOWN = 6_000
ITEM_TEXT_MAX = 240
#: Required fields filled in at least this share; rows reproduced at least this share of those recorded.
FILL_MIN = 0.9
#: A data request's answer is the site's listing when it holds a list of at least this many objects.
LISTING_MIN = 3
#: Pay that is plausible as a yearly figure.
PAY_RANGE = (10_000, 3_000_000)
#: Nights of rows kept as the recent median, and how far under it a night may fall before it looks broken.
HISTORY = 14
DROP_BELOW = 0.4
MEDIAN_NEEDS = 3
#: Nights a recipe may fail in a row before the site is marked stale and a re-record is queued; rewrites that may fail
#: before the owner is told; days between two rewrites of one site.
STALE_AFTER = 2
REWRITES_MAX = 3
REWRITE_EVERY_DAYS = 7


class NotRecorded(skills.SkillError):
    """The recipe asked for an address the recording never made."""


# --- capture ----------------------------------------------------------------------------------------------------------


def _secret_query(url: str) -> bool:
    return any(_SECRET_NAME.search(k) or find_secret(v) for k, v in parse_qsl(urlsplit(url).query))


def _secret_body(post_data: str | None) -> bool:
    if not post_data:
        return False
    try:
        found = json.loads(post_data)
    except ValueError:
        return bool(find_secret(post_data))

    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            return any(_SECRET_NAME.search(str(k)) or walk(v) for k, v in value.items())
        if isinstance(value, list):
            return any(walk(v) for v in value)
        return isinstance(value, str) and bool(find_secret(value))

    return walk(found)


def sanitize(raw: Any, page_url: str, recorded_at: datetime | None = None) -> dict:
    """The capture a recipe may be made from, out of what the browser layer reported (`raw`: a list of
    {method, url, post_data, status, content_type, body}). Returns {version, page_url, recorded_at, responses, dropped}
    where `dropped` counts what was left out and why."""
    dropped: dict[str, int] = {}
    out: list[dict] = []
    seen: set[tuple] = set()
    blobs: list[dict] = []

    def drop(why: str) -> None:
        dropped[why] = dropped.get(why, 0) + 1

    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        if "blob" in r:  # a data block the page's HTML holds (`sites/embedded.py`), not a request
            blobs.append(r)
            continue
        method, url, body = str(r.get("method") or "").upper(), str(r.get("url") or ""), r.get("body")
        ctype = str(r.get("content_type") or "").lower()
        if method not in ("GET", "POST"):
            drop("not a GET or POST")
        elif not public_url(url):
            drop("not a public address")
        elif int(r.get("status") or 0) != 200 or not isinstance(body, str):
            drop("not a JSON answer")
        elif len(body) > BODY_MAX:
            drop("too big")
        elif _secret_query(url) or _secret_body(r.get("post_data")):
            drop("carries a token, key or session")
        else:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict | list):  # data of any type is kept when it is JSON (GraphQL, text/plain)
                drop("not a JSON answer")
                continue
            key = (method, url, r.get("post_data") or "")
            if key in seen:
                continue
            seen.add(key)
            out.append({"method": method, "url": url, "post_data": r.get("post_data") or None, "status": 200,
                        "content_type": ctype, "body": body})
    if len(out) > RESPONSES_MAX:
        drop("over the limit")  # the biggest answers are kept (a job list is one), in the order they were made
        keep = set(sorted(range(len(out)), key=lambda i: -len(out[i]["body"]))[:RESPONSES_MAX])
        out = [r for i, r in enumerate(out) if i in keep]
    total = 0
    for i, r in enumerate(out):  # the biggest answers last: what is over the capture's size in all is left out
        total += len(r["body"])
        if total > CAPTURE_MAX:
            drop("over the size of a capture")
            out = out[:i]
            break
    capture = {"version": CAPTURE_VERSION, "page_url": page_url, "responses": out, "dropped": dropped,
               "recorded_at": (recorded_at or datetime.now(UTC)).isoformat()}
    if blobs and not has_listing(out):  # the fallback: only when no data request carried the listing
        capture["embedded"] = _embedded(blobs, total, drop)
    return capture


def _postings_like(items: list) -> bool:
    """Whether a list of objects looks like job postings: most of its items have a title and an id, address
    or path at the top (a list of video clips or menu entries has not)."""
    def has(item: dict, pattern: str) -> bool:
        return any(re.search(pattern, str(k), re.I) for k in item)
    fits = sum(1 for i in items if has(i, r"title|job_?name|position|role") and has(i, r"id$|url|path|slug|link|req"))
    return fits >= max(2, 2 * len(items) / 3)


def has_listing(responses: list[dict]) -> bool:
    """Whether a data request's answer holds the site's postings (a list of at least `LISTING_MIN` objects that look
    like postings)."""
    for r in responses:
        try:
            if any(len(v) >= LISTING_MIN and _postings_like(v) for _, v in lists_in(json.loads(r["body"]))):
                return True
        except ValueError:
            continue
    return False


def _embedded(blobs: list[dict], used: int, drop: Any) -> list[dict]:
    """The data blocks a recording may keep: on a public address, structured, no token, within the capture's size."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for b in blobs:
        url, name, body = str(b.get("url") or ""), str(b.get("blob") or ""), b.get("body")
        if not embedded.BLOB_NAME.fullmatch(name) or not isinstance(body, str):
            drop("not a data block")
        elif not public_url(url):
            drop("not a public address")
        elif len(body) > BODY_MAX or used + len(body) > CAPTURE_MAX:
            drop("too big")
        elif find_secret(body):
            drop("carries a token, key or session")
        else:
            try:
                structured = embedded.holds_list(json.loads(body))
            except ValueError:
                structured = False
            if not structured:
                drop("not structured data")
            elif (url, name, body) not in seen and len(out) < RESPONSES_MAX:
                seen.add((url, name, body))
                used += len(body)
                out.append({"url": url, "blob": name, "body": body})
    return out


def fingerprint(capture: dict) -> str:
    return hashlib.sha256(json.dumps(capture, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def pack(capture: dict) -> bytes:
    return zlib.compress(json.dumps(capture, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def unpack(blob: bytes) -> dict:
    return json.loads(zlib.decompress(blob))


# --- what the model sees ----------------------------------------------------------------------------------------------


def _cut(value: Any, depth: int = 0) -> Any:
    """A value with long texts cut and deep or long parts shortened, so an item shows its shape."""
    if isinstance(value, str):
        return value if len(value) <= ITEM_TEXT_MAX else value[:ITEM_TEXT_MAX] + f"...[{len(value)} characters]"
    if isinstance(value, list):
        return [_cut(v, depth + 1) for v in value[:3]] + ([f"...[{len(value)} items]"] if len(value) > 3 else [])
    if isinstance(value, dict):
        if depth > 5:
            return "{...}"
        return {k: _cut(v, depth + 1) for k, v in list(value.items())[:40]}
    return value


def lists_in(value: Any, path: str = "") -> list[tuple[str, list]]:
    """Every list of objects in an answer with its dotted path, longest first."""
    found: list[tuple[str, list]] = []
    if isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            found.append((path, value))
    elif isinstance(value, dict):
        for k, v in value.items():
            found += lists_in(v, f"{path}.{k}" if path else str(k))
    return sorted(found, key=lambda f: -len(f[1]))


def trim(capture: dict, limit: int = TRIM_MAX) -> str:
    """The capture as short text for a session: each response's request and the lists in its answer (path, how many,
    the first two items cut). Responses with the longest list come first; what does not fit is left out and said."""
    shown: list[tuple[int, dict]] = []
    for r in capture.get("responses", []):
        answer = json.loads(r["body"])
        lists = lists_in(answer)
        keys = list(answer)[:30] if isinstance(answer, dict) else None
        shown.append((len(lists[0][1]) if lists else 0, {
            "request": f"{r['method']} {r['url']}"
                       + (f"  body: {r['post_data'][:POST_SHOWN]}" if r.get("post_data") else ""),
            "answer_keys": keys,
            "lists": [{"path": p or "(the answer itself)", "count": len(v), "first_items": _cut(v[:2])}
                      for p, v in lists[:3]]}))
    for b in capture.get("embedded", []):
        lists = lists_in(json.loads(b["body"]))
        shown.append((len(lists[0][1]) if lists else 0, {
            "embedded_in_page": b["url"], "blob": b["blob"],
            "lists": [{"path": p or "(the block itself)", "count": len(v), "first_items": _cut(v[:2])}
                      for p, v in lists[:3]]}))
    head = {"page": capture.get("page_url"), "responses_kept": len(shown), "left_out": capture.get("dropped", {})}
    text, left = json.dumps(head), 0
    for _, entry in sorted(shown, key=lambda s: -s[0]):
        piece = json.dumps(entry)
        if len(text) + len(piece) > limit:
            left += 1
            continue
        text += "\n" + piece
    return text + (f"\n({left} more responses left out for length)" if left else "")


# --- replay -----------------------------------------------------------------------------------------------------------


def _norm_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(sorted(parse_qsl(p.query, keep_blank_values=True))), ""))


def _norm_body(post_data: str | None) -> str:
    if not post_data:
        return ""
    try:
        return json.dumps(json.loads(post_data), sort_keys=True)
    except ValueError:
        return post_data


def _bare(value: Any) -> bool:
    """A value that narrows nothing: absent, empty, zero, false, or a first page's number."""
    return value in (None, "", 0, 1, False, "0", "1", "false") or value == {} or value == []


def _compatible(want: dict, have: dict) -> tuple[int, bool]:
    """How many entries a request shares with a recorded one, and whether they differ only in ways a recipe may:
    every entry equal, or bare on either side (a filter left out, a first page's number)."""
    shared, fits = 0, True
    for k in want.keys() | have.keys():
        a, b = want.get(k), have.get(k)
        if a == b:
            shared += 1
        elif not (_bare(a) or _bare(b)):
            fits = False
    return shared, fits


def _empty_lists(value: Any) -> Any:
    if isinstance(value, list):
        return [] if value and all(isinstance(v, dict) for v in value) else value
    if isinstance(value, dict):
        return {k: _empty_lists(v) for k, v in value.items()}
    return value


class ReplayFetcher:
    """Answers a recipe's requests from a capture; nothing is sent. `requests` counts them; `unrecorded` the ones
    answered with an emptied list."""

    deadline = None

    def __init__(self, capture: dict) -> None:
        self.responses = capture.get("responses", [])
        #: A recorded data block is served as the smallest page that holds it.
        self.pages = [{"method": "GET", "url": b["url"], "post_data": None, "content_type": "text/html",
                       "body": embedded.wrap(b["blob"], b["body"]), "blob": b["blob"], "json": b["body"]}
                      for b in capture.get("embedded", [])]
        self.exact = {(r["method"], _norm_url(r["url"]), _norm_body(r.get("post_data"))): r
                      for r in [*self.responses, *self.pages]}
        self.requests = 0
        self.unrecorded = 0
        self.asked: list[str] = []

    def _answer(self, r: dict, body: str, url: str, method: str) -> httpx.Response:
        return httpx.Response(200, content=body.encode("utf-8"), headers={"content-type": r["content_type"]},
                              request=httpx.Request(method, url))

    def _closest(self, method: str, url: str, body: str, strict: bool) -> dict | None:
        """The recorded request of the same endpoint closest to this one. `strict`: only one that differs in ways a
        recipe's first request may (it leaves out what the page's own request narrowed by, a filter the visitor
        applied, and sends a first page's number where the page was on another: see `_compatible`)."""
        want_query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        try:
            want_body = json.loads(body) if body else {}
        except ValueError:
            want_body = None
        best, best_score = None, -1
        for r in [*self.responses, *self.pages]:
            if r["method"] != method or urlsplit(r["url"]).path != urlsplit(url).path:
                continue
            try:
                have_body = json.loads(r["post_data"]) if r.get("post_data") else {}
            except ValueError:
                have_body = None
            if not isinstance(want_body, dict) or not isinstance(have_body, dict):
                if _norm_body(r.get("post_data")) != body:
                    continue
                want_body = have_body = {}
            have_query = dict(parse_qsl(urlsplit(r["url"]).query, keep_blank_values=True))
            parts = [_compatible(want_query, have_query), _compatible(want_body, have_body)]
            if strict and not all(fits for _, fits in parts):
                continue
            score = sum(n for n, _ in parts)
            if score > best_score:
                best, best_score = r, score
        return best

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests += 1
        self.asked.append(f"{method} {url}")
        sent = kwargs.get("json")
        key = (method, _norm_url(url), _norm_body(json.dumps(sent) if sent is not None else None))
        if (r := self.exact.get(key)) is not None:
            return self._answer(r, r["body"], url, method)
        path = urlsplit(url).path
        if self.requests == 1 and (first := self._closest(method, url, key[2], True)) is not None:
            return self._answer(first, first["body"], url, method)
        near = self._closest(method, url, key[2], False)
        if near is None:
            raise NotRecorded(f"the recording has no answer for {method} {urlsplit(url).netloc}{path}")
        self.unrecorded += 1
        if "blob" in near:
            return self._answer(near, embedded.wrap(near["blob"], json.dumps(
                _empty_lists(json.loads(near["json"])))), url, method)
        return self._answer(near, json.dumps(_empty_lists(json.loads(near["body"]))), url, method)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)


def _detail_pattern(template: str) -> re.Pattern:
    # The marker is plain letters: `_norm_url` re-encodes the query, which would turn a control character in it into
    # `%00` and leave the pattern unable to match an id carried in the query (a `finder=ById;Id="{ext_id}"` query).
    marker = "EXTIDMARKER"
    return re.compile(re.escape(_norm_url(template.replace("{ext_id}", marker))).replace(marker, ".+"))


@dataclass
class Verdict:
    """Whether a recipe reproduced a recording."""

    ok: bool
    problems: list[str] = field(default_factory=list)
    rows: int = 0
    recorded: int = 0
    stats: dict = field(default_factory=dict)


def _recorded_rows(recipe: skills.RequestRecipe, capture: dict) -> int:
    """The distinct rows the capture holds at the recipe's list path, in the answers to its endpoint."""
    path = urlsplit(_norm_url(recipe.url)).path
    rows: set[str] = set()
    if recipe.kind == "embedded":
        for b in capture.get("embedded", []):
            if urlsplit(b["url"]).path == path and b["blob"] == recipe.blob:
                found = skills._dig(json.loads(b["body"]), recipe.items)
                if isinstance(found, list):
                    rows |= {json.dumps(x, sort_keys=True) for x in found if isinstance(x, dict)}
        return len(rows)
    for r in capture.get("responses", []):
        if r["method"] == recipe.method and urlsplit(r["url"]).path == path:
            found = skills._dig(json.loads(r["body"]), recipe.items)
            if isinstance(found, list):
                rows |= {json.dumps(x, sort_keys=True) for x in found if isinstance(x, dict)}
    return len(rows)


def quality(postings: list[dict]) -> dict:
    """What a read's postings fill in: the share with a title, an address that is public, a posted date, and pay that is
    plausible when there is any."""
    n = len(postings) or 1
    paid = [p for p in postings if p.get("comp_min") or p.get("comp_max")]
    return {
        "rows": len(postings),
        "title": sum(1 for p in postings if p.get("title")) / n,
        "url": sum(1 for p in postings if p.get("url") and public_url(p["url"])) / n,
        "ext_id": sum(1 for p in postings if p.get("ext_id")) / n,
        "posted": sum(1 for p in postings if p.get("posted")) / n,
        "pay_plausible": (sum(1 for p in paid if PAY_RANGE[0] <= (p.get("comp_max") or p.get("comp_min") or 0)
                              <= PAY_RANGE[1]) / len(paid)) if paid else 1.0,
        "text": sum(1 for p in postings if p.get("text")) / n,
    }


async def verify_offline(recipe_raw: Any, capture: dict) -> Verdict:
    """Run a recipe against the capture and say whether it reproduced the recorded list. Never raises."""
    try:
        recipe = skills.parse_recipe(recipe_raw)
    except ValueError as exc:
        return Verdict(False, [f"the recipe is not valid: {exc}"])
    if not isinstance(recipe, skills.RequestRecipe):
        return Verdict(False, ["a recipe written from a recording is a request or an embedded recipe"])
    problems: list[str] = []
    if recipe.detail is not None:
        pattern = _detail_pattern(recipe.detail.url)
        if not any(pattern.fullmatch(_norm_url(r["url"])) for r in capture.get("responses", [])):
            problems.append("the detail request was never recorded: leave detail out, or use an address the page "
                            "called")
    recorded = _recorded_rows(recipe, capture)
    fetch = ReplayFetcher(capture)
    try:
        listing = await skills.run_recipe(cast(Fetcher, fetch), recipe)
    except (skills.SkillError, ValueError) as exc:
        return Verdict(False, [*problems, f"replaying it failed: {exc}"], 0, recorded)
    stats = quality(listing.postings)
    if recorded == 0:
        problems.append("the recording holds no rows at that endpoint and list path")
    if not listing.postings:
        problems.append("the recipe read no postings from the recording")
    elif recorded and len(listing.postings) < FILL_MIN * recorded:
        problems.append(f"it reproduced {len(listing.postings)} of the {recorded} recorded rows")
    for name in ("title", "url", "ext_id"):
        if listing.postings and stats[name] < FILL_MIN:
            problems.append(f"{name} is filled in for {stats[name]:.0%} of the postings; it must be {FILL_MIN:.0%}")
    if stats["pay_plausible"] < FILL_MIN:
        problems.append("the pay it reads is not plausible as a yearly figure")
    if "posted" in recipe.fields and listing.postings and stats["posted"] < 0.5:
        problems.append("the posted dates do not parse for half of the postings: add a date transform")
    return Verdict(not problems, problems, len(listing.postings), recorded, stats)


async def verify_posting_offline(recipe_raw: Any, capture: dict, sample_url: str) -> Verdict:
    """Run a host's posting recipe against a recording of one posting's page (`sample_url`) and say whether it gave that
    posting's whole text: the recipe covers the sample's host, its pattern names the posting's id in the sample's
    address, its detail request is one the page made (with that id in it), and replaying it reads a whole text. Never
    raises."""
    try:
        recipe = skills.parse_recipe(recipe_raw)
    except ValueError as exc:
        return Verdict(False, [f"the recipe is not valid: {exc}"])
    if not isinstance(recipe, skills.PostingRecipe):
        return Verdict(False, ["a recipe written from a posting's page is a posting recipe (kind: posting)"])
    host = urlsplit(sample_url).hostname or ""
    if not skills.host_covers(recipe.host, host):
        return Verdict(False, [f"host {recipe.host} does not cover the recorded page's host {host}"])
    match = skills.compile_pattern(recipe.url_pattern, "url_pattern").fullmatch(sample_url.split("#", 1)[0])
    if match is None or not match.group("ext_id"):
        return Verdict(False, [f"url_pattern does not match the recorded page's address {sample_url} whole, with the "
                               "posting's id in (?P<ext_id>...)"])
    pattern = _detail_pattern(recipe.detail.url)
    if not any(pattern.fullmatch(_norm_url(r["url"])) for r in capture.get("responses", [])):
        return Verdict(False, ["the detail request was never recorded: use an address the page called, with the "
                               "posting's id where {ext_id} is"])
    replay = ReplayFetcher(capture)
    try:
        got = await skills.read_posting(cast(Fetcher, replay), recipe, sample_url)
    except (skills.SkillError, ValueError) as exc:
        return Verdict(False, [f"replaying it failed: {exc}"])
    if replay.unrecorded:
        return Verdict(False, ["the detail request for the recorded posting was not one the page made: use the "
                               "address the page called, with the posting's id where {ext_id} is"])
    text = (got.text if got is not None else None) or ""
    if not text:
        return Verdict(False, ["the detail request gave no description: check the path of the description"])
    if (why := whole.why_not(text)) is not None:
        return Verdict(False, [f"the text it read is not a whole posting ({why})"])
    return Verdict(True, [], 1, 1, {"text": len(text)})


# --- health -----------------------------------------------------------------------------------------------------------

BLOCKED_STATUSES = (401, 403, 429)


def blocked(exc: BaseException) -> bool:
    """A refusal or a block: a stop that cools the site, never a reason to rewrite the recipe."""
    from app.jobsearch.fetch.polite import Refused

    return isinstance(exc, Refused) or getattr(exc, "status", None) in BLOCKED_STATUSES


def judge_night(history: list[dict], rows: int, stats: dict | None, error: str | None) -> str | None:
    """Why a night's read looks broken, or None. `history` is the earlier nights' entries ({rows, ...}, oldest first).
    Zero rows is a failure, never "nothing new"; a fall far under the recent median is one too."""
    if error:
        return error
    if rows == 0:
        return "it read no postings"
    recent = [h["rows"] for h in history[-HISTORY:] if h.get("rows")]
    if len(recent) >= MEDIAN_NEEDS and rows < DROP_BELOW * statistics.median(recent):
        return f"it read {rows} postings against a recent median of {int(statistics.median(recent))}"
    if stats:
        for name in ("title", "url", "ext_id"):
            if stats.get(name, 1.0) < FILL_MIN:
                return f"{name} is filled in for {stats[name]:.0%} of the postings"
    return None
