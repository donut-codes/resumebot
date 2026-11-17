"""What a runbook says of its site's addresses, and the one reader of it (the generic-sites design, Part 1.2). Code
names the problem, never the site: how a link reads on a site is data in the site's runbook (`Runbook.address`), and the
functions here are the only place that data is understood.

Four lists, each of rules; the key says what a rule that matches means:

  - `one_posting`: the link opens one posting. On a job board the list is the board's whole answer (True when a rule
    matches, False when none does; an empty list says no link on the board is one). On any other site a matching rule
    says True and a site with rules of that `rank` says nothing else: None (the generic rules decide).
  - `board`: the link names one employer's board on this job system; a rule's `capture` is the board's parts (`{}`: a
    link on the system that names no customer, its assets or its sign-in). None of the rules matching: no board.
  - `apply_page`: an application form's link and the posting page that follows from it by a fixed rule (`template`).
  - `board_url`: how to write a link a person can open to see a stored board (`needs`: the parts it takes).

A rule is a mapping of any of these keys (an unknown key is a failure to load, so a typo is never silently no rule):

  - `host`: a regular expression the whole host must match.
  - `customer_host`: the first name of the host is a customer's, not the vendor's (`_address.VENDOR_HOSTS`).
  - `path`: a regular expression the whole path (its non-empty parts joined by `/`) must match, in any case. Where a
    posting's id is a segment, `<id_like>` stands for the pattern of one (`_address.id_like`). `skip_locale`: a part
    that is a language (`en-US`) is left out of the path first.
  - `query`: a mapping from a query key (lower case) to what its value must be: `any` (not empty), `digits`
    (ASCII digits) or `word` (letters, digits, `_`, `.` and `-`, all of it).
  - `capture`: a mapping from a part's name to where it comes from: `host`, `host_name` (the first name of the host:
    `acme` of `acme.example.test`), `query:<key>` or `group:<name>` (a named group of `path`). A source with nothing in
    it leaves the part out.
  - `capture` of `ext_id`, in a `one_posting` rule: the posting's id, for a site whose link alone is enough to read the
    posting (`posting_of`).
  - `form`: in a `one_posting` rule that captures an `ext_id`: the link is the posting's application form. The posting
    is read from it, but it is not the page of the posting's own (`first` over the shapes leaves it out).
  - `lower`: names of `capture`d parts that are kept in lower case (an id the site writes in either case).
  - `encoded`: names of `capture`d parts that are percent-encoded (an id from a query, put in another link).
  - `rank`: only in `one_posting` of a site that is not a job board: where among the generic shapes the rule is asked
    (`address._shaped`, `address._job_system_shape`), 0 first. Each rank is asked once.
  - `template`: the link an `apply_page` rule gives, with its `capture`d parts and `{host}`.

Pure and standard library only. It reads a runbook's data and an address and nothing else, and imports no reader."""

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

KEYS = ("one_posting", "board", "apply_page", "board_url")
_RULE_KEYS = frozenset(
    {"host", "customer_host", "path", "skip_locale", "query", "capture", "rank", "template", "needs", "form", "lower",
     "encoded"})
_QUERY = frozenset({"any", "digits", "word"})
_WORD = re.compile(r"[A-Za-z0-9_.-]+")
_SOURCE = re.compile(r"^(?:host|host_name|query:[a-z0-9_]+|group:[A-Za-z_][A-Za-z0-9_]*)$")
#: The pattern of one posting's id as a path segment: all digits, three or more digits in it, a GUID (any trailing
#: text, as `_address.GUID.match` has it), or a requisition code (`R0012`, `JR-101`).
_ID_LIKE = (r"(?:\d+|[^/]*\d{3,}[^/]*|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}[^/]*|"
            r"[A-Za-z]{1,4}[-_]?\d{2,})")


@dataclass(frozen=True)
class Rule:
    host: "re.Pattern[str] | None" = None
    customer_host: bool = False
    path: "re.Pattern[str] | None" = None
    skip_locale: bool = False
    query: tuple[tuple[str, str], ...] = ()
    capture: tuple[tuple[str, str], ...] = ()
    rank: int = 0
    template: str | None = None
    needs: tuple[str, ...] = ()
    form: bool = False
    lower: tuple[str, ...] = ()
    encoded: tuple[str, ...] = ()


@dataclass(frozen=True)
class Subject:
    """One link as the rules see it: its host, its path's parts (in the case written) and its query (lower-case keys,
    the first value of each)."""

    host: str
    parts: tuple[str, ...]
    query: Mapping[str, str]

    def path(self, skip_locale: bool = False) -> str:
        if skip_locale:
            from app.jobsearch.adapters._address import LOCALE

            return "/".join(p for p in self.parts if not LOCALE.match(p))
        return "/".join(self.parts)


def _compile(pattern: Any, what: str) -> "re.Pattern[str]":
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{what} is a regular expression")
    try:
        return re.compile(pattern.replace("<id_like>", _ID_LIKE), re.I)
    except re.error as exc:
        raise ValueError(f"{what} is not a regular expression ({exc})") from exc


def compile_rule(raw: object) -> Rule:
    """One rule of a runbook, checked and compiled; ValueError says what is wrong with it."""
    if not isinstance(raw, dict):
        raise ValueError("a rule is a mapping")
    if unknown := sorted(set(raw) - _RULE_KEYS):
        raise ValueError(f"a rule has no key {unknown[0]!r}")
    query = raw.get("query", {})
    capture = raw.get("capture", {})
    if not isinstance(query, dict) or any(v not in _QUERY or not isinstance(k, str) or k != k.lower()
                                          for k, v in query.items()):
        raise ValueError(f"query is a mapping from a lower-case key to one of {sorted(_QUERY)}")
    if not isinstance(capture, dict) or any(not isinstance(v, str) or not _SOURCE.match(v) for v in capture.values()):
        raise ValueError("capture is a mapping from a part's name to host, host_name, query:<key> or group:<name>")
    rank = raw.get("rank", 0)
    needs = raw.get("needs", [])
    template = raw.get("template")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < 0:
        raise ValueError("rank is a whole number, 0 or more")
    if not isinstance(needs, list) or any(not isinstance(n, str) for n in needs):
        raise ValueError("needs is a list of a board's part names")
    if template is not None:
        if not isinstance(template, str):
            raise ValueError("template is text")
        try:
            names = {f for _, f, _, _ in string.Formatter().parse(template) if f}
        except ValueError as exc:
            raise ValueError(f"template is not a template ({exc})") from exc
        if names - set(capture) - {"host"} - set(needs):
            raise ValueError("a template uses only the parts it captures, or needs, and {host}")
    form, lower = raw.get("form", False), raw.get("lower", [])
    if not isinstance(form, bool) or (form and not any(n == "ext_id" for n in capture)):
        raise ValueError("form is true or false, and goes with a capture of ext_id")
    if not isinstance(lower, list) or any(n not in capture for n in lower):
        raise ValueError("lower is a list of captured parts")
    encoded = raw.get("encoded", [])
    if not isinstance(encoded, list) or any(n not in capture for n in encoded):
        raise ValueError("encoded is a list of captured parts")
    path = _compile(raw["path"], "path") if "path" in raw else None
    for name in (v[6:] for v in capture.values() if v.startswith("group:")):
        if path is None or name not in path.groupindex:
            raise ValueError(f"capture names the group {name!r}, which path does not have")
    return Rule(
        host=_compile(raw["host"], "host") if "host" in raw else None,
        customer_host=raw.get("customer_host") is True, path=path, skip_locale=raw.get("skip_locale") is True,
        query=tuple(sorted(query.items())), capture=tuple(capture.items()), rank=rank, template=template,
        needs=tuple(needs), form=form, lower=tuple(lower), encoded=tuple(encoded))


def check(address: object) -> None:
    """The shape of a runbook's `address`: a failure to load (ValueError) when one rule is wrong."""
    if not isinstance(address, dict):
        raise ValueError("address is a mapping")
    for key, value in address.items():
        if key == "own_careers_paths":
            continue
        if key not in KEYS:
            raise ValueError(f"address has no key {key!r}")
        if not isinstance(value, list):
            raise ValueError(f"address.{key} is a list of rules")
        for i, raw in enumerate(value):
            try:
                compile_rule(raw)
            except ValueError as exc:
                raise ValueError(f"address.{key}[{i}]: {exc}") from exc


_compiled: dict[tuple[int, str], tuple[object, tuple[Rule, ...] | None]] = {}


def rules(runbook: Any, key: str) -> tuple[Rule, ...] | None:
    """The compiled rules of one list of a runbook's `address`; None when it has no such list (an empty list is not
    none: it says "no rule matches"). Compiled once per runbook. The runbook is held, so its identity stays its own."""
    held = _compiled.get((id(runbook), key))
    if held is None:
        raw = runbook.address.get(key)
        held = (runbook, None if raw is None else tuple(compile_rule(r) for r in raw))
        _compiled[(id(runbook), key)] = held
    return held[1]


def _value_ok(kind: str, value: str | None) -> bool:
    if not value:
        return False
    if kind == "word":
        return _WORD.fullmatch(value) is not None
    return kind == "any" or (value.isascii() and value.isdigit())


def match(rule: Rule, subject: Subject) -> "re.Match[str] | bool":
    """Whether a rule holds of a link: the path's match (so its groups can be read), or True for a rule with no path;
    False when it does not."""
    if rule.host is not None and not rule.host.fullmatch(subject.host):
        return False
    if rule.customer_host:
        from app.jobsearch.adapters._address import is_customer_host

        if not is_customer_host(subject.host):
            return False
    if any(not _value_ok(kind, subject.query.get(key)) for key, kind in rule.query):
        return False
    if rule.path is None:
        return True
    found = rule.path.fullmatch(subject.path(rule.skip_locale))
    return found if found is not None else False


def first(found: tuple[Rule, ...], subject: Subject,
          rank: int | None = None) -> "tuple[Rule, re.Match[str] | bool] | None":
    """The first rule (of this rank, when one is given) that holds of the link, with its match."""
    for rule in found:
        if rank is not None and rule.rank != rank:
            continue
        if (hit := match(rule, subject)) is not False:
            return rule, hit
    return None


def has_rank(found: tuple[Rule, ...], rank: int) -> bool:
    return any(r.rank == rank for r in found)


def captured(rule: Rule, hit: "re.Match[str] | bool", subject: Subject) -> dict[str, str]:
    """The parts a rule's `capture` takes from the link; a part with nothing in it is left out."""
    out: dict[str, str] = {}
    for name, source in rule.capture:
        if source == "host":
            value = subject.host
        elif source == "host_name":
            value = subject.host.split(".")[0]
        elif source.startswith("query:"):
            value = subject.query.get(source[6:], "")
        else:
            value = hit.group(source[6:]) if isinstance(hit, re.Match) else None
        if value:
            value = value.lower() if name in rule.lower else value
            out[name] = quote(value, safe="") if name in rule.encoded else value
    return out


def board_url(found: tuple[Rule, ...], board: Mapping[str, Any]) -> str | None:
    """The link a rule with every part of `needs` in the board gives; None when no rule has what it takes."""
    for rule in found:
        if rule.template and all(board.get(n) for n in rule.needs):
            return rule.template.format(**{n: board[n] for n in rule.needs})
    return None


def posting_of(found: tuple[Rule, ...], subject: Subject, given: str) -> tuple[str, str] | None:
    """The one posting a link opens, when a `one_posting` rule that captures an `ext_id` holds of it: its id and the
    address to read it from (`given`, the link as it was written). None for any other link: no such rule holds, or the
    id it names is empty."""
    for rule in found:
        if not any(name == "ext_id" for name, _ in rule.capture):
            continue
        if (hit := match(rule, subject)) is False:
            continue
        if ext_id := captured(rule, hit, subject).get("ext_id"):
            return ext_id, given
    return None


def shaped(found: tuple[Rule, ...], subject: Subject) -> bool:
    """Whether a link has the shape of one posting's own page: a `one_posting` rule holds of it, an application form
    (`form`) being no such page."""
    return any(not rule.form and match(rule, subject) is not False for rule in found)
