"""Results data embedded in a page's HTML (the fallback of a recording that saw no data request): pure text work, no
network, no browser, and nothing here runs what a page holds.

Some sites render their first results on the server and keep them in the page as data: a `<script id="..."
type="application/json">` block, a JSON-LD block, or a script that assigns an object to a name
(`window.state = {...};`). Only structured data counts: a block must parse as JSON and hold a list of objects. A block
is named, in a recipe's `blob`, by where it is: `script:<id>`, `ldjson:<n>` (the n-th JSON-LD block, from 0) or
`var:<dotted name>`. `find_blobs` lists the blocks of a page; `extract` reads one by name; `wrap` builds the minimal
page that holds a recorded block so the one executor can replay a recipe against a recording."""

import json
import re
from typing import Any

#: A block must be at least this long to be worth naming, and hold a list of at least this many objects.
BLOB_MIN_CHARS = 200
LIST_MIN = 2
#: How many blocks one page may give.
BLOBS_MAX = 12
BLOB_NAME = re.compile(r"(script|ldjson|var):[A-Za-z0-9_$.\-]{1,100}")

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ASSIGN = re.compile(r"(?<![\w$.])((?:window\.)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*=\s*(?=[{\[])")
_DECODER = json.JSONDecoder()


def _attrs(text: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _ATTR.finditer(text)}


def holds_list(value: Any, depth: int = 0) -> bool:
    """Whether parsed JSON holds a list of at least `LIST_MIN` objects."""
    if isinstance(value, list):
        if len(value) >= LIST_MIN and all(isinstance(v, dict) for v in value):
            return True
        return depth < 8 and any(holds_list(v, depth + 1) for v in value[:50])
    if isinstance(value, dict):
        return depth < 8 and any(holds_list(v, depth + 1) for v in value.values())
    return False


def _json_scripts(html: str) -> list[tuple[dict[str, str], str]]:
    return [(_attrs(m.group(1)), m.group(2)) for m in _SCRIPT.finditer(html)]


def _is_ld(attrs: dict[str, str]) -> bool:
    return "ld+json" in attrs.get("type", "").lower()


def _is_json(attrs: dict[str, str]) -> bool:
    return "json" in attrs.get("type", "").lower()


def _assigned(text: str, name: str | None = None):
    """(name, value, json text) of each `name = {...}` or `name = [...]` in a script, in order."""
    for m in _ASSIGN.finditer(text):
        if name is not None and m.group(1) != name:
            continue
        try:
            value, end = _DECODER.raw_decode(text, m.end())
        except ValueError:
            continue
        yield m.group(1), value, text[m.end():end]


def find_blobs(html: str) -> list[dict]:
    """The structured blocks of a page, in the order they occur: [{blob, body}] with `body` the block's JSON text."""
    out: list[dict] = []
    seen: set[str] = set()
    ld = 0
    for attrs, text in _json_scripts(html):
        if len(out) >= BLOBS_MAX:
            break
        if _is_ld(attrs):
            index, ld = ld, ld + 1
            name = f"ldjson:{index}"
        elif _is_json(attrs) and re.fullmatch(r"[A-Za-z0-9_$.\-]{1,100}", attrs.get("id", "")):
            name = f"script:{attrs['id']}"
        elif not attrs.get("src") and not _is_json(attrs):
            for var, value, raw in _assigned(text):
                bn = f"var:{var.removeprefix('window.')}"
                if bn not in seen and len(raw) >= BLOB_MIN_CHARS and holds_list(value) and len(out) < BLOBS_MAX:
                    seen.add(bn)
                    out.append({"blob": bn, "body": raw})
            continue
        else:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if name not in seen and len(text.strip()) >= BLOB_MIN_CHARS and holds_list(value):
            seen.add(name)
            out.append({"blob": name, "body": text.strip()})
    return out


def extract(html: str, blob: str) -> Any:
    """The parsed block `blob` names in a page, or None when the page has no such block."""
    kind, _, name = blob.partition(":")
    ld = 0
    for attrs, text in _json_scripts(html):
        if kind == "ldjson" and _is_ld(attrs):
            if str(ld) == name:
                try:
                    return json.loads(text)
                except ValueError:
                    return None
            ld += 1
        elif kind == "script" and _is_json(attrs) and attrs.get("id") == name:
            try:
                return json.loads(text)
            except ValueError:
                return None
        elif kind == "var" and not attrs.get("src") and not _is_json(attrs):
            for _, value, _ in _assigned(text, name):
                return value
            for _, value, _ in _assigned(text, f"window.{name}"):
                return value
    return None


def wrap(blob: str, body: str) -> str:
    """The smallest page that holds a recorded block where `extract` finds it."""
    safe = body.replace("</", "<\\/")
    kind, _, name = blob.partition(":")
    if kind == "script":
        return f'<html><script id="{name}" type="application/json">{safe}</script></html>'
    if kind == "ldjson":
        empty = '<script type="application/ld+json">null</script>' * int(name or 0)
        return f'<html>{empty}<script type="application/ld+json">{safe}</script></html>'
    return f"<html><script>{name} = {safe};</script></html>"
