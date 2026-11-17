#!/usr/bin/env python3
"""Built to share (docs/PRD.md 2.8): the repository names no one's machine, address or search.

Stdlib only; reads the files git tracks, changes nothing. Two kinds of check, neither of which names a person:

  Shape rules (always, in CI too). A line may not hold
    - a private network address (10.x, 172.16-31.x, 192.168.x), except in test files, which use them to test the
      app's refusal of such addresses;
    - a path under the root user's home, a Windows or macOS user folder, or any Linux home but the scout image's;
    - an e-mail address at a real domain: fixtures use the reserved names (example, test, invalid, localhost).
      The mail-login and mail-reading test files may use a public mailbox provider, which that code tells apart.
  - a path under a named user's home folder (a drive, then Users, then the name; /home/<name>/; /Users/<name>/), unless the name is a
    neutral one (someone, user, you, name, username, example) or a placeholder (<name>, %USERNAME%, $USER);
  - in a test file, a private address other than the neutral ones: 10.0.0.x, 192.168.0.x, 192.168.1.x, 172.16.0.x.
  A section of a file between lines holding `maintainer:start` and `maintainer:end` is skipped for the other rules,
  but never for a named user's
  home folder or a private address: those name a real machine wherever they sit.

  Two more rules, outside a maintainer section: a private memory note id (the name of a private notes tool followed by an n-number)
  may appear nowhere, and the agent instructions and templates (app/instructions/, app/templates/) may not narrate the
  owner as he, his or him (say "the owner", "you" or "they").

  Private terms (only where the file exists). The words that must never reach the repository (your own names, places,
  employers, pay figures) cannot be listed in it, so they live in a file of your own outside the checkout:
      ${XDG_CONFIG_HOME:-~/.config}/resumebot/private-terms.txt      or  --terms FILE
  One term per line, `#` for comments, matched whole and without regard to case in every tracked file except the
  folders below. With no such file the check runs the shape rules only and says so. A hit names the file and line,
  never the term.

CI runs the shape rules.

  The site-name rule (always, in CI too). Code, comments, docs, tests, fixtures and file paths name the problem, never
  a site: the names are not listed here but derived at run time from app/backend/app/sites/site_terms.json and from
  every runbook's name, key and hosts (shipped, restricted and pending). Where a name may stand:
    - site_terms.json, the restricted folder (app/backend/restricted_runbooks/), a shipped runbook for its own site, the
      plugin (plugins/resumebot/) and the extension (resumebot-browser-plugin/);
    - the code (not the comments or docstrings) of the released migrations frozen in scripts/site-names-allow.txt, each
      by the digest of check_migrations.code_of: change the code and the check fails;
    - what scripts/site-names-allow.txt suppresses: a path, with the hash of its content, and the reason.
  Today's hits are in scripts/site-names-baseline.txt (`path:count`, `path:path` for a name in the path itself): the
  check fails only on a hit that is not in it, and every lane deletes its own lines until the file is empty.
  A hit prints `path:line: site-name`, never the name. `--write-baseline` rewrites the baseline from the current hits.

  `--exported DIR` checks an exported tree on the site rule alone: the names may stand only in site_terms.json and in a
  shipped runbook for its own site; no baseline, no migration allowance.

Usage: python scripts/check_no_one.py [--terms FILE] [--write-baseline]
       python scripts/check_no_one.py --exported DIR
"""

import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path


#: This file and its test define the rules, so they hold examples of what the rules forbid.
EXEMPT = {"scripts/check_no_one.py", "scripts/test_check_no_one.py"}
#: Not text to read: generated lock files.
SKIP_NAMES = {"package-lock.json", "requirements.lock", "requirements-dev.lock"}
#: Other people's code and the plugin's and extension's own examples are not the shared app: the private terms are not
#: sought there (the shape rules still are, outside .claude/).
TERMS_SKIP_PREFIXES = (".claude/", "plugins/", "resumebot-browser-plugin/")
SHAPE_SKIP_PREFIXES = (".claude/",)
#: The Census Bureau's places file is public data about every US place, so a town name of yours is in it by right. So is
#: a city of yours in Unicode's table of every Windows time zone and its standard name (rows only, no code).
TERMS_SKIP_FILES = {"app/backend/app/jobsearch/measures/data/us_places.csv", "app/backend/app/windows_zones.py"}
#: Fixtures for the mail reader and the mail login tell a public mailbox provider from a company's own domain.
PUBLIC_MAILBOX_FILES = {
    "app/backend/tests/mailmine/test_mail_reader.py",
    "app/backend/tests/jobsearch/test_mail_login.py",
    "app/backend/tests/split/test_page_writes.py",
    "app/frontend/src/components/agents/MailLogin.test.tsx",
}
PUBLIC_MAILBOX_DOMAINS = {"gmail.com", "outlook.com", "yahoo.com"}
RESERVED_LABELS = {"example", "test", "invalid", "localhost"}
FILE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "css", "js", "ts", "tsx", "map", "woff", "woff2"}

PRIVATE_ADDRESS = re.compile(
    r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])"
)
HOME_PATH = re.compile(
    r"(?<![\w.])/root/|(?<![\w.])/(?:Users|home)/(?!node\b)[\w.-]+|(?<![\w])[A-Za-z]:[\\/]+Users[\\/]+",
)
#: A home folder with a user name in it; the lookahead lets the neutral names and the placeholders (<name>, %USERNAME%,
#: $USER: none starts with a word character) through.
NEUTRAL_USER_NAMES = r"(?:someone|user|you|name|username|example|node)(?![\w.-])"
NAMED_HOME_PATH = re.compile(
    r"(?<![\w.])/(?:Users|home)/(?!" + NEUTRAL_USER_NAMES + r")[\w.-]+"
    r"|(?<![\w])[A-Za-z]:[\\/]+Users[\\/]+(?!" + NEUTRAL_USER_NAMES + r")[\w.-]+",
)
#: The private addresses test files may use: the ones a home router hands out by default, which name no one's network.
NEUTRAL_TEST_ADDRESS = re.compile(r"^(?:10\.0\.0\.\d+|10\.1\.2\.3|192\.168\.[01]\.\d+|172\.16\.0\.\d+)$")
#: A private memory note id: the name of a private notes tool, then an n-number.
MEMORY_NOTE_ID = re.compile(r"\bmuninn\b\W{0,3}n\d{3,}\b", re.IGNORECASE)
#: Instruction and template prose speaks of the owner as "the owner", "you" or "they", never as he, his or him.
NARRATION_PREFIXES = ("app/instructions/", "app/templates/")
NARRATION = re.compile(r"\b(?:he|his|him|himself)\b", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.([A-Za-z]{2,}))")


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return bool({"tests", "test"} & set(parts[:-1])) or name.startswith("test_") or ".test." in name or ".spec." in name


def lines_with_maintainer_flag(text: str) -> list[tuple[int, str, bool]]:
    """The (line number, line, inside) triples, `inside` true between `maintainer:start` and `maintainer:end`."""
    out, inside = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if "maintainer:start" in line:
            inside = True
        elif "maintainer:end" in line:
            inside = False
        else:
            out.append((n, line, inside))
    return out


def term_pattern(term: str) -> re.Pattern:
    left = r"(?<![A-Za-z0-9_])" if re.match(r"\w", term[0]) else ""
    right = r"(?![A-Za-z0-9_])" if re.match(r"\w", term[-1]) else ""
    return re.compile(left + re.escape(term) + right, re.IGNORECASE)


def read_terms(path: Path) -> list[re.Pattern]:
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(term_pattern(line))
    return terms


def named_machine_problems(path: str, line: str) -> list[str]:
    """What names a real machine wherever it sits, a maintainer section included."""
    found = []
    if NAMED_HOME_PATH.search(line):
        found.append("a path under a named user's home folder")
    for m in PRIVATE_ADDRESS.finditer(line):
        if not is_test_file(path):
            what = "a private network address"
        elif not NEUTRAL_TEST_ADDRESS.match(m.group(0)):
            what = "a private network address that is not one of the neutral test addresses"
        else:
            continue
        if what not in found:
            found.append(what)
    return found


def shape_problems(path: str, line: str) -> list[str]:
    found = []
    if HOME_PATH.search(line):
        found.append("a path under a user's home folder")
    for m in EMAIL.finditer(line):
        domain, tld = m.group(1).lower(), m.group(2).lower()
        if tld in FILE_EXTENSIONS or RESERVED_LABELS & set(domain.split(".")):
            continue
        if domain in PUBLIC_MAILBOX_DOMAINS and path in PUBLIC_MAILBOX_FILES:
            continue
        found.append("an e-mail address at a real domain (fixtures use example, test, invalid or localhost)")
    return found


def problems(path: str, text: str, terms: list[re.Pattern] | None) -> list[str]:
    """The problems in one file: 'path:line: what', never the term that matched."""
    if path in EXEMPT or path.rsplit("/", 1)[-1] in SKIP_NAMES:
        return []
    out = []
    for n, line, inside in lines_with_maintainer_flag(text):
        if not path.startswith(SHAPE_SKIP_PREFIXES):
            whats = named_machine_problems(path, line)
            if not inside:
                # A named home is also "a path under a user's home folder": say it once, by the more exact name.
                whats += [w for w in shape_problems(path, line) if not (whats and w.startswith("a path under a user"))]
            out += [f"{path}:{n}: {what}" for what in whats]
        if inside:
            continue
        if MEMORY_NOTE_ID.search(line):
            out.append(f"{path}:{n}: a private memory note id")
        if path.startswith(NARRATION_PREFIXES) and NARRATION.search(line):
            out.append(f"{path}:{n}: the owner narrated as he, his or him (say the owner, you or they)")
        if terms and path not in TERMS_SKIP_FILES and not path.startswith(TERMS_SKIP_PREFIXES) and any(t.search(line) for t in terms):
            out.append(f"{path}:{n}: a term from your private-terms file")
    return out


# ----------------------------------------------------------------------------------------------------------------------
# The site-name rule. No name is written here: they are read from the terms file and the runbooks at run time.

TERMS_PATH = "app/backend/app/sites/site_terms.json"
SHIPPED_RUNBOOKS = "app/backend/app/sites/runbooks/"
RESTRICTED_DIR = "app/backend/restricted_runbooks/"
RUNBOOK_DIRS = (SHIPPED_RUNBOOKS, RESTRICTED_DIR + "runbooks/", RESTRICTED_DIR + "pending/")
#: Where any site's name may stand (the owner's list, 2026-11-15). The slash after the plugin's name keeps its setup
#: sibling out; .claude/, evals/, the docs and the specs are not here on purpose.
SITE_FREE_PREFIXES = (RESTRICTED_DIR, "plugins/resumebot/", "resumebot-browser-plugin/")
#: Fields only a runbook in the restricted folder may carry; a kept (exported) runbook must not.
RESTRICTED_FIELDS = ("reader", "robots", "signed_in", "extras")
ALLOW_FILE = "scripts/site-names-allow.txt"
BASELINE_FILE = "scripts/site-names-baseline.txt"
SITE_RULE = "site-name (a site's name: code names the problem, never the site)"
SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac"}


@dataclass
class SiteNames:
    #: lower-case name -> the ids of the sites that own it ("t:<terms entry>", "r:<runbook path>")
    sites: dict[str, set[str]] = field(default_factory=dict)
    #: runbook path -> the site ids that runbook may name
    own: dict[str, set[str]] = field(default_factory=dict)
    #: matches a lower-case name in lower-cased text
    pattern: re.Pattern | None = None

    @property
    def empty(self) -> bool:
        return self.pattern is None


def host_stem(host: str) -> str:
    """The registrable domain's own label: the part of a host a site is known by."""
    labels = host.lower().strip(".").split(".")
    if len(labels) < 2:
        return labels[0]
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2]


def covers(a: str, b: str) -> bool:
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def trie_pattern(words: list[str]) -> str:
    """One pattern for many words, the longest match first, by their shared beginnings: far faster than an alternation."""
    root: dict = {}
    for word in words:
        node = root
        for ch in word:
            node = node.setdefault(ch, {})
        node[""] = {}

    def build(node: dict) -> str:
        branches = [re.escape(ch) + build(sub) for ch, sub in sorted(node.items()) if ch]
        if not branches:
            return ""
        body = branches[0] if len(branches) == 1 and "" not in node else "(?:" + "|".join(branches) + ")"
        return body + ("?" if "" in node else "")

    return build(root)


def build_site_names(terms: list, runbooks: dict[str, dict]) -> SiteNames:
    """The names to keep out of the repository, from a terms document (a list of entries) and runbooks (path -> document)."""
    names = SiteNames()
    entry_hosts: dict[str, list[str]] = {}

    def add(name, site: str) -> None:
        if isinstance(name, str) and len(name.strip()) >= 2:
            names.sites.setdefault(name.strip().lower(), set()).add(site)

    def add_hosts(hosts, site: str) -> list[str]:
        good = [h.strip().lower() for h in hosts if isinstance(h, str) and h.strip()] if isinstance(hosts, list) else []
        for host in good:
            add(host, site)
            add(host_stem(host), site)
        return good

    for entry in terms if isinstance(terms, list) else []:
        if isinstance(entry, dict):
            site = f"t:{entry.get('name')}"
            add(entry.get("name"), site)
            entry_hosts[site] = add_hosts(entry.get("hosts"), site)
    for path, doc in runbooks.items():
        if not isinstance(doc, dict):
            continue
        site = f"r:{path}"
        add(doc.get("name"), site)
        add(doc.get("key"), site)
        hosts = add_hosts(doc.get("hosts"), site)
        names.own[path] = {site} | {t for t, ths in entry_hosts.items() if any(covers(h, th) for h in hosts for th in ths)}
    if names.sites:
        names.pattern = re.compile(trie_pattern(list(names.sites)))
    return names


def load_site_names(root: Path) -> SiteNames:
    terms: list = []
    terms_file = root / TERMS_PATH
    if terms_file.is_file():
        terms = json.loads(terms_file.read_text(encoding="utf-8"))
    runbooks: dict[str, dict] = {}
    for folder in RUNBOOK_DIRS:
        for file in sorted((root / folder).glob("*.json")):
            try:
                runbooks[f"{folder}{file.name}"] = json.loads(file.read_text(encoding="utf-8"))
            except ValueError:
                pass  # scripts/classify_runbooks.py reports a runbook that is not JSON
    return build_site_names(terms, runbooks)


def name_hits(text: str, names: SiteNames) -> list[tuple[int, int, str]]:
    """(line, column, lower-case name) of each name in the text, whole names only: a name inside a longer word is not
    one, but a camel-case join is (a name then a capital letter, or a capital letter then the name)."""
    out: list[tuple[int, int, str]] = []
    if names.pattern is None:
        return out
    lowered = text.lower()
    if len(lowered) != len(text):
        lowered = text.translate({c: c + 32 for c in range(65, 91)})
    pos = line_start = 0
    line = 1
    while (m := names.pattern.search(lowered, pos)):
        start, end = m.span()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        left = not before.isalnum() or (before.islower() and text[start].isupper())
        right = not after.isalnum() or (after.isupper() and text[end - 1].islower())
        if left and right:
            line += text.count("\n", line_start, start)
            line_start = start
            out.append((line, start - (text.rfind("\n", 0, start) + 1), m.group(0)))
            pos = end
        else:
            pos = start + 1
    return out


@dataclass
class Allowance:
    kind: str  # file: the hash of the content; code: the check_migrations.code_of digest; name: the path itself may carry one
    value: str


def read_allowances(file: Path) -> dict[str, list[Allowance]]:
    """`PATH KIND VALUE REASON...` per line; `#` for comments."""
    out: dict[str, list[Allowance]] = {}
    if file.is_file():
        for line in file.read_text(encoding="utf-8").splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3 and not line.lstrip().startswith("#"):
                out.setdefault(parts[0], []).append(Allowance(parts[1], parts[2]))
    return out


def content_hash(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def code_digest(text: str) -> str | None:
    """The digest of what a migration does, by check_migrations.code_of (the function that defines its code)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import check_migrations  # only a frozen migration needs it, so a tree without it can still run the other rules
    except ImportError:
        return None
    try:
        return hashlib.sha256(check_migrations.code_of(text).encode("utf-8")).hexdigest()
    except (SyntaxError, ValueError):
        return None


def outside_code(text: str) -> tuple[set[int], list[tuple[int, int, int]]]:
    """The lines of a Python file's docstrings, and its comments as (line, first column, last column)."""
    docstring_lines: set[int] = set()
    comments: list[tuple[int, int, int]] = []
    try:
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    docstring_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.start[1], tok.end[1]))
    except (SyntaxError, ValueError, tokenize.TokenError):
        return set(), []
    return docstring_lines, comments


def may_name_anything(path: str, exported: bool) -> bool:
    if path == TERMS_PATH:
        return True
    return not exported and path.startswith(SITE_FREE_PREFIXES)


def own_sites(path: str, names: SiteNames) -> set[str]:
    return names.own.get(path, set()) if path.startswith(SHIPPED_RUNBOOKS) else set()


def site_hits(path: str, text: str, names: SiteNames, allowed: dict[str, list[Allowance]] | None = None,
              exported: bool = False) -> tuple[list[int], list[str]]:
    """(the line of each site name in a file's content, hard failures). `allowed` is the allowance file: a file's
    content hash suppresses its hits while the content is unchanged; outside an exported tree, a released migration's
    frozen code digest lets a name stand in its code (not in its comments or docstrings), and a changed digest is a
    failure."""
    if names.empty or may_name_anything(path, exported) or path in (ALLOW_FILE, BASELINE_FILE):
        return [], []  # the two data files list site-named paths by design
    own = own_sites(path, names)
    hits = [h for h in name_hits(text, names) if not (names.sites[h[2]] & own)]
    failures: list[str] = []
    for rule in (allowed or {}).get(path, []):
        if rule.kind == "file" and hits and rule.value == content_hash(text):
            hits = []
        elif rule.kind == "code" and not exported and hits:
            if rule.value == code_digest(text):
                docstring_lines, comments = outside_code(text)
                hits = [h for h in hits
                        if h[0] in docstring_lines or any(h[0] == c[0] and c[1] <= h[1] < c[2] for c in comments)]
            else:
                failures.append(f"{path}:1: {SITE_RULE}: this released migration's code changed from the digest frozen in {ALLOW_FILE}")
    return [h[0] for h in hits], failures


def path_hit(path: str, names: SiteNames, allowed: dict[str, list[Allowance]] | None = None, exported: bool = False) -> bool:
    if names.empty or may_name_anything(path, exported):
        return False
    if not exported and any(r.kind == "name" for r in (allowed or {}).get(path, [])):
        return False
    own = own_sites(path, names)
    return any(not (names.sites[h[2]] & own) for h in name_hits(path, names))


def read_baseline(file: Path) -> tuple[dict[str, int], set[str]]:
    """(`path:count` lines: the names in a file's content; the `path:path` lines: a name in the path itself)."""
    counts: dict[str, int] = {}
    paths: set[str] = set()
    if file.is_file():
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                path, _, what = line.rpartition(":")
                if what.isdigit():
                    counts[path] = int(what)
                else:
                    paths.add(path)
    return counts, paths


@dataclass
class SiteScan:
    lines: dict[str, list[int]] = field(default_factory=dict)  # path -> the line of each name in its content
    paths: set[str] = field(default_factory=set)  # paths that carry a name themselves
    failures: list[str] = field(default_factory=list)


def scan_site_names(files: list[tuple[str, str | None]], names: SiteNames, allowed: dict[str, list[Allowance]] | None,
                    exported: bool = False) -> SiteScan:
    """`files` are (path, text), the text None for a file that is not read as text."""
    scan = SiteScan()
    for path, text in files:
        if path_hit(path, names, allowed, exported):
            scan.paths.add(path)
        if text is None or path.rsplit("/", 1)[-1] in SKIP_NAMES:
            continue
        lines, failures = site_hits(path, text, names, allowed, exported)
        scan.failures += failures
        if lines:
            scan.lines[path] = lines
    return scan


def new_site_problems(scan: SiteScan, counts: dict[str, int], paths: set[str]) -> list[str]:
    """Hard failures, a path with a name that the baseline does not list, and every hit of a file whose count is above
    its baseline (which hit is the new one cannot be told)."""
    out = list(scan.failures)
    for path in sorted(scan.paths):
        if path not in paths:
            out.append(f"{path}:0: {SITE_RULE}: in the file's path")
    for path, lines in sorted(scan.lines.items()):
        allowed = counts.get(path, 0)
        if len(lines) > allowed:
            out += [f"{path}:{n}: {SITE_RULE}" + (f" ({len(lines)} hits, baseline {allowed})" if i == 0 else "")
                    for i, n in enumerate(lines)]
    return out


def baseline_text(scan: SiteScan) -> str:
    rows = sorted([f"{p}:path" for p in scan.paths] + [f"{p}:{len(v)}" for p, v in scan.lines.items()])
    head = ["# Today's site-name hits: `path:count`, or `path:path` for a name in the file's own path.",
            "# The check fails only on a hit not listed here. Delete your lines as you remove the names; never add one.",
            "# Rewrite from the current hits: python scripts/check_no_one.py --write-baseline"]
    return "\n".join(head + rows) + "\n"


def exported_problems(root: Path) -> list[str] | None:
    """The site rule over an exported tree: names only in the terms file and in a shipped runbook for its own site, and
    no restricted folder or restricted field in what is kept. None when the tree has no terms file."""
    if not (root / TERMS_PATH).is_file():
        return None
    names = load_site_names(root)
    allowed = read_allowances(root / ALLOW_FILE)
    files: list[tuple[str, str | None]] = []
    extra: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        for filename in filenames:
            file = Path(dirpath) / filename
            path = file.relative_to(root).as_posix()
            text = None
            if file.stat().st_size <= 2_000_000:
                data = file.read_bytes()
                if b"\0" not in data[:8192]:
                    text = data.decode("utf-8", errors="replace")
            files.append((path, text))
            if "restricted_runbooks" in path:
                extra.append(f"{path}:0: restricted-folder: the restricted runbooks never ship")
            elif path.startswith(SHIPPED_RUNBOOKS) and path.endswith(".json") and text is not None:
                try:
                    doc = json.loads(text)
                except ValueError:
                    continue
                extra += [f"{path}:1: restricted-field: a kept runbook may not carry `{f}`"
                          for f in RESTRICTED_FIELDS if isinstance(doc, dict) and f in doc]
    return extra + new_site_problems(scan_site_names(files, names, allowed, exported=True), {}, set())


def tracked_files(root: Path) -> list[str]:
    raw = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    return [p for p in raw.decode("utf-8").split("\0") if p]


def default_terms_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "resumebot" / "private-terms.txt"


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    if argv[:1] == ["--exported"]:
        if len(argv) != 2 or not Path(argv[1]).is_dir():
            print("usage: check_no_one.py --exported DIR", file=sys.stderr)
            return 2
        found = exported_problems(Path(argv[1]))
        if found is None:
            print(f"check_no_one: {argv[1]} has no {TERMS_PATH}: nothing to check the names against", file=sys.stderr)
            return 2
        for line in found:
            print(line, file=sys.stderr)
        print(f"check_no_one: exported tree, {len(found)} problem(s)" if found else "check_no_one: exported tree clean")
        return 1 if found else 0
    terms_file = default_terms_file()
    rest = list(argv)
    write_baseline = "--write-baseline" in rest
    if write_baseline:
        rest.remove("--write-baseline")
    if rest[:1] == ["--terms"] and len(rest) == 2:
        terms_file = Path(rest[1])
    elif rest:
        print("usage: check_no_one.py [--terms FILE] [--write-baseline] | --exported DIR", file=sys.stderr)
        return 2
    terms = None
    if terms_file.is_file():
        terms = read_terms(terms_file)
        print(f"check_no_one: shape rules, and {len(terms)} private terms from {terms_file}")
    else:
        print(f"check_no_one: no private-terms file at {terms_file}: shape rules only")
    names = load_site_names(root)
    allowed = read_allowances(root / ALLOW_FILE)
    found = []
    site_files: list[tuple[str, str | None]] = []
    for path in tracked_files(root):
        file = root / path
        data = file.read_bytes() if file.is_file() and file.stat().st_size <= 2_000_000 else b"\0"
        if b"\0" in data[:8192]:
            site_files.append((path, None))
            continue
        text = data.decode("utf-8", errors="replace")
        site_files.append((path, text))
        found += problems(path, text, terms)
    scan = scan_site_names(site_files, names, allowed)
    if write_baseline:
        (root / BASELINE_FILE).write_text(baseline_text(scan), encoding="utf-8", newline="\n")
        print(f"check_no_one: baseline rewritten: {len(scan.paths)} paths, {len(scan.lines)} files with names in them")
    else:
        counts, paths = read_baseline(root / BASELINE_FILE)
        found += new_site_problems(scan, counts, paths)
        shrunk = [p for p in paths if p not in scan.paths] + [p for p, c in counts.items() if len(scan.lines.get(p, [])) < c]
        if shrunk:
            print(f"check_no_one: {len(shrunk)} baseline line(s) can shrink or go: run --write-baseline")
    for line in found:
        print(line, file=sys.stderr)
    if found:
        print(f"check_no_one: {len(found)} problem(s): the repository must name no one's machine, address, search or site",
              file=sys.stderr)
        return 1
    print("check_no_one: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
