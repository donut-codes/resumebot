"""The layout of the resume and the cover letter, as data (`templates/document.<name>.layout.yaml`), and the one walk
that turns a layout and a packet's text into the lines of a document. The Word and the PDF writer
(`docs_render.py`) each draw those same lines, so neither can drift from the other and a layout change is an edit to
the file, not to code. A missing or malformed file stops the app at start-up and names the file."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import settings


class LayoutError(Exception):
    """A layout file is missing or malformed; the message names the file and what is wrong."""


@dataclass(frozen=True)
class Part:
    """A run of text in one line."""

    text: str
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True)
class Line:
    """One line of a document. `kind` is "para", "heading" or "bullet". `plain` lines are one run set in one style (the
    Word writer marks that run bold or not, as it always has); the others are runs of mixed style. `style` holds only
    what the layout set: size, align, before, after, keep, rule."""

    kind: str
    parts: tuple[Part, ...]
    style: dict[str, Any] = field(default_factory=dict)
    plain: bool = True


@dataclass(frozen=True)
class Layout:
    name: str
    page: dict[str, Any]
    heading: dict[str, Any]
    bullet: dict[str, Any]
    blocks: list[dict[str, Any]]


NUMBER = (int, float)
_STYLE: dict[str, Any] = {"size": NUMBER, "bold": bool, "italic": bool, "align": str, "before": NUMBER,
                          "after": NUMBER, "keep": bool, "upper": bool, "rule": dict}
_RULE: dict[str, Any] = {"space": NUMBER, "width": NUMBER, "color": str}
_PAGE: dict[str, Any] = {"font": str, "size": NUMBER, "after": NUMBER, "side_margin": NUMBER, "end_margin": NUMBER}
# kind: (required keys, other keys with their types). The kinds that draw lines of their own also take style keys.
_KINDS: dict[str, tuple[set[str], dict[str, Any]]] = {
    "line": ({"field"}, {"join": str, "always": bool, "format": str}),
    "paragraphs": ({"field"}, {}),
    "recipient": ({"lines"}, {"between": NUMBER}),
    "section": ({"heading", "body", "field"}, {}),
}
_SECTIONS: dict[str, dict[str, Any]] = {
    "text": {},
    "bullets": {},
    "competencies": {"label": dict, "label_suffix": str, "items_join": str},
    "roles": {"employer": dict, "employer_join": str, "title": dict, "dates_join": str, "dates_prefix": str,
              "descriptor": dict, "intro": dict},
    "education": {"degree": dict, "school_prefix": str},
}
_SECTION_REQUIRED = {"competencies": {"label_suffix", "items_join"},
                     "roles": {"employer_join", "dates_join", "dates_prefix"}, "education": {"school_prefix"}}


def _ok(value: Any, kinds: Any) -> bool:
    """`value` is of `kinds`; a true or false is never taken for a number."""
    return isinstance(value, kinds) and (kinds is bool or not isinstance(value, bool))


def _check_style(style: Any, where: str, fail) -> None:
    if not isinstance(style, dict):
        fail(f"{where} must be a mapping")
    for key, value in style.items():
        if key not in _STYLE:
            fail(f"{where}: unknown style key '{key}'")
        if not _ok(value, _STYLE[key]):
            fail(f"{where}: '{key}' has the wrong kind of value")
    if style.get("align", "left") not in ("center", "left"):
        fail(f"{where}: align must be center or left")
    for key, value in style.get("rule", {}).items():
        if key not in _RULE or not _ok(value, _RULE[key]):
            fail(f"{where}: bad rule setting '{key}'")


def _check_block(block: Any, n: int, fail) -> None:
    where = f"block {n}"
    if not isinstance(block, dict) or block.get("kind") not in _KINDS:
        fail(f"{where} needs a kind of {', '.join(_KINDS)}")
    kind = block["kind"]
    required, extra = _KINDS[kind]
    for key in required:
        if key not in block:
            fail(f"{where} ({kind}) needs '{key}'")
    for key, kinds in extra.items():
        if key in block and not _ok(block[key], kinds):
            fail(f"{where}: '{key}' has the wrong kind of value")
    rest = {k: v for k, v in block.items() if k not in {"kind"} | required | set(extra)}
    if kind == "section":
        body = block["body"]
        if body not in _SECTIONS:
            fail(f"{where}: body must be one of {', '.join(_SECTIONS)}")
        if not isinstance(block["heading"], str):
            fail(f"{where}: heading must be text")
        for key in _SECTION_REQUIRED.get(body, set()):
            if key not in block:
                fail(f"{where}: '{key}' is missing")
        for key, kinds in _SECTIONS[body].items():
            if key in rest:
                if kinds is dict:
                    _check_style(rest[key], f"{where} {key}", fail)
                elif not _ok(rest[key], kinds):
                    fail(f"{where}: '{key}' has the wrong kind of value")
                del rest[key]
        if rest:
            fail(f"{where}: unknown key '{sorted(rest)[0]}'")
    else:
        _check_style(rest, where, fail)
    if kind == "recipient":
        lines = block["lines"]
        if not isinstance(lines, list) or not all(
                isinstance(x, dict) and isinstance(x.get("field"), str) and set(x) <= {"field", "format"}
                for x in lines):
            fail(f"{where}: lines must be a list of fields, each with an optional format")


def parse_layout(name: str, text: str, source: str) -> Layout:
    """The layout in `text`, or a LayoutError that names `source`."""
    def fail(why: str):
        raise LayoutError(f"{source}: {why}")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LayoutError(f"{source}: not readable YAML ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("page"), dict) or not isinstance(data.get("blocks"), list):
        fail("needs a 'page' mapping and a 'blocks' list")
    unknown = set(data) - {"page", "heading", "bullet", "blocks"}
    if unknown:
        fail(f"unknown top-level key '{sorted(unknown)[0]}'")
    page = data["page"]
    for key, kinds in _PAGE.items():
        if key not in page or not _ok(page[key], kinds):
            fail(f"page needs '{key}' as the right kind of value")
    if set(page) - set(_PAGE):
        fail(f"page: unknown key '{sorted(set(page) - set(_PAGE))[0]}'")
    for key in ("heading", "bullet"):
        _check_style(data.get(key, {}), key, fail)
    for n, block in enumerate(data["blocks"], 1):
        _check_block(block, n, fail)
    if any(b["kind"] == "section" for b in data["blocks"]) and "heading" not in data:
        fail("sections need a 'heading' style")
    return Layout(name, page, data.get("heading", {}), data.get("bullet", {}), data["blocks"])


def load_layout(name: str, folder: Path | None = None) -> Layout:
    """`templates/document.<name>.layout.yaml`, read and checked."""
    path = Path(folder or settings.templates_path) / f"document.{name}.layout.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LayoutError(f"{path}: cannot be read ({exc.strerror or exc})") from exc
    return parse_layout(name, text, str(path))


# --- the walk ---------------------------------------------------------------------------------------------------------

_LINE_KEYS = ("size", "align", "before", "after", "keep", "rule")


def _look(style: dict[str, Any]) -> dict[str, Any]:
    """The part of a style that is about the line, not the type of its text."""
    return {k: style[k] for k in _LINE_KEYS if style.get(k) is not None}


def _text(value: Any, join: str = " ") -> str:
    if isinstance(value, (list, tuple)):
        return join.join(str(x) for x in value)
    return str(value) if value else ""


def _plain(text: str, style: dict[str, Any], kind: str = "para") -> Line:
    return Line(kind, (Part(text, bool(style.get("bold")), bool(style.get("italic"))),), _look(style))


def _runs(parts: list[tuple[str, dict[str, Any]]], style: dict[str, Any]) -> Line:
    return Line("para", tuple(Part(t, bool(s.get("bold")), bool(s.get("italic"))) for t, s in parts), _look(style),
                plain=False)


def _section(layout: Layout, block: dict[str, Any], rows: Any) -> list[Line]:
    heading = block["heading"].upper() if layout.heading.get("upper") else block["heading"]
    out = [_plain(heading, layout.heading, "heading")]
    body = block["body"]
    if body == "text":
        out.append(_plain(str(rows), {}))
    elif body == "bullets":
        out += [_plain(a.get("text", ""), layout.bullet, "bullet") for a in rows]
    elif body == "competencies":
        for row in rows:
            out.append(_runs([(f"{row.get('area', '')}{block['label_suffix']}", block.get("label", {})),
                              (block["items_join"].join(row.get("items", [])), {})], {}))
    elif body == "roles":
        for role in rows:
            employer = block["employer_join"].join(x for x in (role.get("employer"), role.get("location")) if x)
            out.append(_plain(employer, block.get("employer", {})))
            dates = block["dates_join"].join(x for x in (role.get("start"), role.get("end")) if x)
            title = block.get("title", {})
            parts = [(role.get("title", ""), title)]
            if dates:
                parts.append((f"{block['dates_prefix']}{dates}", {}))
            out.append(_runs(parts, title))
            if role.get("descriptor"):
                out.append(_plain(role["descriptor"], block.get("descriptor", {})))
            if role.get("intro"):
                out.append(_plain(role["intro"], block.get("intro", {})))
            out += [_plain(b.get("text", ""), layout.bullet, "bullet") for b in role.get("bullets", [])]
    else:  # education
        for e in rows:
            parts = [(e.get("degree", ""), block.get("degree", {}))]
            if e.get("school"):
                parts.append((f"{block['school_prefix']}{e['school']}", {}))
            out.append(_runs(parts, {}))
    return out


def flow(layout: Layout, values: dict[str, Any]) -> list[Line]:
    """The lines of the document, in order: the layout's blocks filled from `values`, a block with nothing to show left
    out. Both writers draw exactly this list."""
    out: list[Line] = []
    for block in layout.blocks:
        kind = block["kind"]
        style = {k: v for k, v in block.items() if k in _STYLE}
        if kind == "section":
            if values.get(block["field"]):
                out += _section(layout, block, values[block["field"]])
        elif kind == "line":
            text = _text(values.get(block["field"]), block.get("join", " "))
            if text and "format" in block:
                text = block["format"].format(text)
            if text or block.get("always"):
                out.append(_plain(text, style))
        elif kind == "paragraphs":
            out += [_plain(t, style) for t in values.get(block["field"]) or [] if t]
        else:  # recipient
            texts = []
            for spec in block["lines"]:
                text = _text(values.get(spec["field"]))
                if text:
                    texts.append(spec["format"].format(text) if "format" in spec else text)
            for i, text in enumerate(texts):
                after = style.get("after") if i == len(texts) - 1 else block.get("between")
                out.append(_plain(text, {**style, "after": after}))
    return out
