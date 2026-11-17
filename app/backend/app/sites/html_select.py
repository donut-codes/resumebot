"""A small CSS selector engine for HTML recipes (docs/PRD.md path 49.2): the recipe's selectors are run over a page the
app fetched itself, without a browser, so the engine is the app's own, standard library only, and its grammar is closed:
what the recorder writes and nothing a page could turn into a program.

**Grammar.** One or more comma-separated selectors; each is compound parts joined by a space (a descendant) or `>` (a
child). A compound part is an optional tag and any of `#id`, `.class`, `[attr]`, `[attr=v]`, `[attr^=v]`, `[attr$=v]`,
`[attr*=v]` (the value quoted or bare, `i` after it for case-blind) and `:nth-of-type(n)`; it names at least one of
them. Anything else (`*`, `:not`, `+`, `~`, pseudo-elements, a brace or a script word) is refused by `check` with a
ValueError.

**The tree.** `parse` reads HTML tolerantly (a page's own mistakes are not ours): void tags have no children, a `li`,
`p`, `td`, `tr`, `option`, `dt` or `dd` that is not closed ends where the next of its kind starts, a stray end tag is
ignored, and nesting deeper than `DEPTH_MAX` is flattened. No script runs; `script`, `style`, `template` and `noscript`
hold no text. Nothing here recurses over the page, so a hostile page cannot overflow the stack."""

import re
from html.parser import HTMLParser

#: The longest selector (as in `skills.SELECTOR_MAX`), the deepest nesting kept, the most HTML read and elements kept.
SELECTOR_MAX = 200
DEPTH_MAX = 400
HTML_MAX = 8_000_000
ELEMENTS_MAX = 400_000

VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
                  "track", "wbr"})
SILENT = frozenset({"script", "style", "template", "noscript", "head"})
#: Tags that start a new line of text.
BLOCKS = frozenset({"address", "article", "aside", "blockquote", "br", "dd", "details", "div", "dl", "dt", "fieldset",
                    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
                    "main", "nav", "ol", "p", "pre", "section", "table", "tr", "ul"})
#: What a start tag of a key ends when it is the open element on top.
ENDS = {"li": {"li"}, "p": {"p"}, "td": {"td", "th"}, "th": {"td", "th"}, "tr": {"td", "th", "tr"},
        "option": {"option"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}}


class Node:
    """An element: its tag, attributes (lowercase names, values as written), parent and children (elements and text)."""

    __slots__ = ("tag", "attrs", "parent", "children", "index", "seen")

    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None") -> None:
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node | str] = []
        self.index = 0  # its place among its parent's elements of the same tag, from 1
        self.seen: dict[str, int] = {}  # how many children of each tag it has had

    def get(self, name: str) -> str | None:
        return self.attrs.get(name.lower())

    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def elements(self):
        """Every element below this one, in document order (no recursion)."""
        stack = [c for c in reversed(self.children) if isinstance(c, Node)]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(c for c in reversed(node.children) if isinstance(c, Node))

    def text(self) -> str:
        """The words under this element as a visitor reads them: block elements and `br` start a line, runs of spaces
        are one, blank lines are dropped; script, style and the like hold none."""
        parts: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
                continue
            if item.tag in SILENT:
                continue
            if item.tag in BLOCKS:
                stack.append("\n")
            stack.extend(reversed(item.children))
            if item.tag in BLOCKS:
                parts.append("\n")
        lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
        return "\n".join(line for line in lines if line)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        top = self.stack[-1]
        closes = ENDS.get(tag)
        if closes:
            while len(self.stack) > 1 and self.stack[-1].tag in closes:
                self.stack.pop()
            top = self.stack[-1]
        if self.count >= ELEMENTS_MAX:
            return
        self.count += 1
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs if k}, top)
        node.index = top.seen[tag] = top.seen.get(tag, 0) + 1
        top.children.append(node)
        if tag not in VOID and len(self.stack) < DEPTH_MAX:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and len(self.stack) > 1 and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def parse(html: str) -> Node:
    """The page as a tree. Never raises: a page that is no HTML at all is an empty tree."""
    builder = _Builder()
    try:
        builder.feed(str(html or "")[:HTML_MAX])
        builder.close()
    except Exception:  # a page's own mistakes are not ours
        pass
    return builder.root


# --- selectors ----------------------------------------------------------------------------------------------------

_IDENT = r"-?[A-Za-z_][\w-]*"
_VALUE = r"\"[^\"]{0,80}\"|'[^']{0,80}'|[^\]\s\"'\\]{1,80}"
_PART = re.compile(
    rf"(?P<tag>{_IDENT})|#(?P<id>{_IDENT})|\.(?P<cls>{_IDENT})"
    rf"|\[\s*(?P<attr>{_IDENT})\s*(?:(?P<op>[\^$*]?=)\s*(?P<val>{_VALUE})(?P<ci>\s+i)?)?\s*\]"
    r"|:nth-of-type\(\s*(?P<nth>\d{1,3})\s*\)")


_STEP = re.compile(r"\s*>\s*|\s+")


class _Compound:
    def __init__(self) -> None:
        self.tag: str | None = None
        self.id: str | None = None
        self.classes: list[str] = []
        self.attrs: list[tuple[str, str | None, str | None, bool]] = []
        self.nth: int | None = None


def _split(selector: str) -> list[str]:
    """The comma-separated selectors, a comma inside brackets or quotes left alone."""
    out, depth, quote, last = [], 0, "", 0
    for i, ch in enumerate(selector):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(selector[last:i])
            last = i + 1
    out.append(selector[last:])
    return out


def _compile_one(text: str) -> list[tuple[str, _Compound]]:
    """The compounds of one selector with the combinator that joins each to the one before it (the first has " ")."""
    text = text.strip()
    if not text:
        raise ValueError("a selector is empty")
    chain: list[tuple[str, _Compound]] = []
    pos, comb = 0, " "
    while pos < len(text):
        compound, start = _Compound(), pos
        while pos < len(text):
            m = _PART.match(text, pos)
            if m is None:
                break
            pos = m.end()
            if m["tag"]:
                if compound.tag or compound.id or compound.classes or compound.attrs or compound.nth:
                    raise ValueError("a tag comes first in a compound selector")
                compound.tag = m["tag"].lower()
            elif m["id"]:
                compound.id = m["id"]
            elif m["cls"]:
                compound.classes.append(m["cls"])
            elif m["attr"]:
                val = m["val"]
                if val and val[0] in "\"'":
                    val = val[1:-1]
                compound.attrs.append((m["attr"].lower(), m["op"], val, bool(m["ci"])))
            else:
                compound.nth = int(m["nth"])
        if pos == start:
            raise ValueError(f"selector not allowed here: {text[pos:pos + 20]!r}")
        chain.append((comb, compound))
        if pos >= len(text):
            break
        step = _STEP.match(text, pos)
        if step is None:
            raise ValueError(f"selector not allowed here: {text[pos:pos + 20]!r}")
        comb, pos = (">" if ">" in step.group() else " "), step.end()
        if pos >= len(text):
            raise ValueError("a selector cannot end on a combinator")
    return chain


_CACHE: dict[str, list[list[tuple[str, _Compound]]]] = {}


def compile_selector(selector: str) -> list[list[tuple[str, _Compound]]]:
    """The selector as its alternatives, each a chain of compounds. ValueError says why a selector is not allowed."""
    if not isinstance(selector, str) or not selector.strip() or len(selector) > SELECTOR_MAX:
        raise ValueError(f"a selector is 1 to {SELECTOR_MAX} characters")
    got = _CACHE.get(selector)
    if got is None:
        got = [_compile_one(part) for part in _split(selector)]
        if len(_CACHE) > 500:
            _CACHE.clear()
        _CACHE[selector] = got
    return got


def check(selector: str) -> str:
    """The selector as given when it is inside the grammar, else ValueError."""
    compile_selector(selector)
    return selector


def _attr_ok(node: Node, name: str, op: str | None, value: str | None, blind: bool) -> bool:
    have = node.attrs.get(name)
    if have is None:
        return False
    if op is None:
        return True
    want = value or ""
    if blind:
        have, want = have.lower(), want.lower()
    if op == "=":
        return have == want
    if op == "^=":
        return bool(want) and have.startswith(want)
    if op == "$=":
        return bool(want) and have.endswith(want)
    return bool(want) and want in have


def _is(node: Node, c: _Compound) -> bool:
    if c.tag and node.tag != c.tag:
        return False
    if c.id and node.attrs.get("id") != c.id:
        return False
    if c.classes and not set(c.classes) <= set(node.classes()):
        return False
    if c.nth and node.index != c.nth:
        return False
    return all(_attr_ok(node, *a) for a in c.attrs)


def _matches(node: Node, chain: list[tuple[str, _Compound]], at: int) -> bool:
    """Whether `node` is the element the compound `chain[at]` names and the compounds before it hold above it."""
    if not _is(node, chain[at][1]):
        return False
    if at == 0:
        return True
    comb = chain[at][0]
    above = node.parent
    if comb == ">":
        return above is not None and above.tag != "#root" and _matches(above, chain, at - 1)
    while above is not None and above.tag != "#root":
        if _matches(above, chain, at - 1):
            return True
        above = above.parent
    return False


def matches(node: Node, selector: str) -> bool:
    return any(_matches(node, chain, len(chain) - 1) for chain in compile_selector(selector))


def select(root: Node, selector: str) -> list[Node]:
    """Every element below `root` the selector names, in document order."""
    chains = compile_selector(selector)
    return [n for n in root.elements() if any(_matches(n, chain, len(chain) - 1) for chain in chains)]


def select_one(root: Node, selector: str) -> Node | None:
    chains = compile_selector(selector)
    return next((n for n in root.elements() if any(_matches(n, chain, len(chain) - 1) for chain in chains)), None)


def closest(node: Node, selector: str) -> Node | None:
    """The element itself or the nearest ancestor the selector names, or None."""
    compile_selector(selector)
    here: Node | None = node
    while here is not None and here.tag != "#root":
        if matches(here, selector):
            return here
        here = here.parent
    return None
