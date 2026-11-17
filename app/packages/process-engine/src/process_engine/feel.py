"""A small, safe subset of FEEL (BPMN's expression language) for gateway conditions (requirement 15.3).

No Python `eval`, no clock, no randomness: the same condition over the same data gives the same answer, and
every condition can be checked against the process's data model when the file loads.

Supported:
- literals: numbers, "strings" (or 'strings'), `true`, `false`, `null`, lists `[1, 2]`;
- paths: `plan.items`, `result.verdict` (a missing field reads as null);
- comparisons `=` `!=` `<` `<=` `>` `>=`, `x in list`, arithmetic `+ - * /`;
- `and`, `or`, `not(...)` with FEEL's three-valued logic (a null makes the answer null unless the other side
  decides it; a null condition counts as "no");
- `count(list)`, `contains(text, part)`;
- `every x in list satisfies cond`, `some x in list satisfies cond`.

Whatever text or data it is given, `parse`, `analyze`, `evaluate` and `evaluate_condition` raise `ConditionError` and
nothing else, so a caller needs one `except` to turn a bad condition into a plain refusal. That holds because every
way a condition could get big is capped (see the limits below), and because arithmetic that can't be answered is an
error, not a quiet null: a number too large to work with raises `ConditionError` (a condition that quietly chose
"no" over a figure it couldn't read would send the work the wrong way; failing says what happened).
"""

import math
import re
from dataclasses import dataclass

from process_engine.datamodel import DataModel, parent_path
from process_engine.errors import ConditionError

#: Longest condition, in characters.
MAX_LENGTH = 2000
#: Most brackets, calls and quantifiers one inside another while reading.
MAX_DEPTH = 40
#: Most levels in the tree that reading builds. A long chain (`a + b + c + ...`, or a run of minus signs) is shallow
#: while it is read but one level per term once built, and everything that walks the tree walks it level by level.
MAX_TREE_DEPTH = 100
#: Most `every` / `some` one inside another. Each one multiplies the work by the length of its list.
MAX_QUANT_NESTING = 3
#: Most steps one evaluation may take. A step is one part of the condition worked out once; a quantifier works its
#: condition out once per item. Real conditions over real lists take a few thousand.
MAX_STEPS = 50_000
#: Most digits in a number written into a condition (data can hold any number; this is about what a person types).
MAX_NUMBER_DIGITS = 18
#: Largest result of arithmetic, in bits (about 1e77). Past it the answer is an error.
MAX_RESULT_BITS = 256
KEYWORDS = {"and", "or", "in", "every", "some", "satisfies", "true", "false", "null"}


# --- syntax tree ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Lit:
    value: object


@dataclass(frozen=True)
class ListLit:
    items: tuple


@dataclass(frozen=True)
class Path:
    root: str
    segs: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return ".".join((self.root, *self.segs))


@dataclass(frozen=True)
class Neg:
    operand: object


@dataclass(frozen=True)
class Bin:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple


@dataclass(frozen=True)
class Quant:
    kind: str  # "every" | "some"
    var: str
    source: object
    cond: object


# --- reading ----------------------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:(?P<num>[0-9]+(?:\.[0-9]+)?)"""
    r"""|(?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
    r"""|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"""
    r"""|(?P<op><=|>=|!=|[=<>+\-*/()\[\],.]))"""
)
#: What a backslash does in a string: `\n`, `\r` and `\t` are the control characters; any other character after one
#: (a quote, a backslash) stands for itself.
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise ConditionError(f"can't read '{text[pos:pos + 12].strip()}' in the condition")
        pos = match.end()
        kind = match.lastgroup
        out.append((kind, match.group(kind)))
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks = _tokens(text)
        self.i = 0
        self.depth = 0  # primaries inside primaries: every way to nest passes through `p_primary`
        self.quants = 0  # `every` / `some` inside `every` / `some`

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise ConditionError("the condition ends too soon")
        self.i += 1
        return tok

    def is_op(self, value: str, offset: int = 0) -> bool:
        tok = self.peek(offset)
        return tok is not None and tok[0] == "op" and tok[1] == value

    def is_word(self, value: str, offset: int = 0) -> bool:
        tok = self.peek(offset)
        return tok is not None and tok[0] == "name" and tok[1] == value

    def expect_op(self, value: str) -> None:
        if not self.is_op(value):
            got = self.peek()
            raise ConditionError(f"expected '{value}' but found '{got[1] if got else 'the end'}'")
        self.i += 1

    def enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ConditionError("the condition is nested too deeply")

    def leave(self) -> None:
        self.depth -= 1

    def parse(self):
        node = self.p_or()
        if self.peek() is not None:
            raise ConditionError(f"unexpected '{self.peek()[1]}' in the condition")
        return node

    def p_or(self):
        node = self.p_and()
        while self.is_word("or"):
            self.i += 1
            node = Bin("or", node, self.p_and())
        return node

    def p_and(self):
        node = self.p_cmp()
        while self.is_word("and"):
            self.i += 1
            node = Bin("and", node, self.p_cmp())
        return node

    def p_cmp(self):
        node = self.p_add()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in ("=", "!=", "<", "<=", ">", ">="):
            self.i += 1
            return Bin(tok[1], node, self.p_add())
        if self.is_word("in"):
            self.i += 1
            return Bin("in", node, self.p_add())
        return node

    def p_add(self):
        node = self.p_mul()
        while self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            node = Bin(op, node, self.p_mul())
        return node

    def p_mul(self):
        node = self.p_unary()
        while self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            node = Bin(op, node, self.p_unary())
        return node

    def p_unary(self):
        signs = 0
        while self.is_op("-"):  # a loop, not a recursion: a run of minus signs can't exhaust the stack
            self.i += 1
            signs += 1
            if signs > MAX_TREE_DEPTH:
                raise ConditionError("the condition is nested too deeply")
        node = self.p_primary()
        for _ in range(signs):
            node = Neg(node)
        return node

    def p_primary(self):
        self.enter()
        node = self.p_atom()
        self.leave()
        return node

    def p_atom(self):
        kind, value = self.take()
        if kind == "num":
            if len(value.partition(".")[0]) > MAX_NUMBER_DIGITS:
                raise ConditionError(f"the number {value[:12]}... has more than {MAX_NUMBER_DIGITS} digits")
            return Lit(float(value) if "." in value else int(value))
        if kind == "str":
            return Lit(re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), value[1:-1]))
        if kind == "op":
            if value == "(":
                node = self.p_or()
                self.expect_op(")")
                return node
            if value == "[":
                items = []
                if not self.is_op("]"):
                    items.append(self.p_or())
                    while self.is_op(","):
                        self.i += 1
                        items.append(self.p_or())
                self.expect_op("]")
                return ListLit(tuple(items))
            raise ConditionError(f"unexpected '{value}' in the condition")
        # a name
        if value == "true":
            return Lit(True)
        if value == "false":
            return Lit(False)
        if value == "null":
            return Lit(None)
        if value in ("every", "some"):
            var_tok = self.take()
            if var_tok[0] != "name" or var_tok[1] in KEYWORDS:
                raise ConditionError(f"'{value}' needs a name for each item, e.g. {value} item in list satisfies ...")
            if not self.is_word("in"):
                raise ConditionError(f"'{value} {var_tok[1]}' needs 'in' and a list")
            self.i += 1
            self.quants += 1
            if self.quants > MAX_QUANT_NESTING:
                raise ConditionError(f"'every' and 'some' can be inside one another only {MAX_QUANT_NESTING} deep")
            source = self.p_add()
            if not self.is_word("satisfies"):
                raise ConditionError(f"'{value}' needs 'satisfies' and a condition")
            self.i += 1
            cond = self.p_or()
            self.quants -= 1
            return Quant(value, var_tok[1], source, cond)
        if value in KEYWORDS:
            raise ConditionError(f"'{value}' can't be used here")
        if self.is_op("("):
            self.i += 1
            args = []
            if not self.is_op(")"):
                args.append(self.p_or())
                while self.is_op(","):
                    self.i += 1
                    args.append(self.p_or())
            self.expect_op(")")
            if value not in FUNCTIONS:
                names = ", ".join(sorted(FUNCTIONS))
                raise ConditionError(f"'{value}(...)' is not a function a condition can use ({names})")
            if len(args) != FUNCTIONS[value][0]:
                raise ConditionError(f"{value}() takes {FUNCTIONS[value][0]} value(s), not {len(args)}")
            return Call(value, tuple(args))
        segs = []
        while self.is_op(".") and self.peek(1) and self.peek(1)[0] == "name":
            self.i += 1
            segs.append(self.take()[1])
        return Path(value, tuple(segs))


def _children(node) -> tuple:
    if isinstance(node, ListLit):
        return node.items
    if isinstance(node, Neg):
        return (node.operand,)
    if isinstance(node, Bin):
        return (node.left, node.right)
    if isinstance(node, Call):
        return node.args
    if isinstance(node, Quant):
        return (node.source, node.cond)
    return ()


def tree_depth(tree) -> int:
    """How many levels the tree has (a lone value is 1). It keeps its own stack, so it can measure any tree; it stops
    counting once the tree is deeper than `MAX_TREE_DEPTH`."""
    deepest = 0
    stack = [(tree, 1)]
    while stack:
        node, level = stack.pop()
        deepest = max(deepest, level)
        if level > MAX_TREE_DEPTH:
            break
        stack.extend((kid, level + 1) for kid in _children(node))
    return deepest


def parse(text: str):
    """Read a condition into a syntax tree. Raises `ConditionError` naming what is wrong, and nothing else."""
    if not isinstance(text, str):
        raise ConditionError("a condition is written as text")
    text = text.strip()
    if text.startswith("="):  # Zeebe-style FEEL expressions open with '='
        text = text[1:].strip()
    if not text:
        raise ConditionError("the condition is empty")
    if len(text) > MAX_LENGTH:
        raise ConditionError(f"the condition is longer than {MAX_LENGTH} characters")
    try:
        tree = _Parser(text).parse()
    except RecursionError:  # the limits above keep it from happening; this is the backstop
        raise ConditionError("the condition is nested too deeply") from None
    if tree_depth(tree) > MAX_TREE_DEPTH:
        raise ConditionError("the condition is nested too deeply (a chain of too many parts)")
    return tree


# --- running ----------------------------------------------------------------------------------------------

def _is_num(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_num(a) and _is_num(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_eq(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _bool(v):
    return v if isinstance(v, bool) else None


def _and(a, b):
    a, b = _bool(a), _bool(b)
    if a is False or b is False:
        return False
    return True if a is True and b is True else None


def _or(a, b):
    a, b = _bool(a), _bool(b)
    if a is True or b is True:
        return True
    return False if a is False and b is False else None


def _count(items):
    return len(items) if isinstance(items, list) else None


def _contains(text, part):
    return part in text if isinstance(text, str) and isinstance(part, str) else None


def _not(v):
    v = _bool(v)
    return None if v is None else not v


#: name -> (argument count, function)
FUNCTIONS = {"count": (1, _count), "contains": (2, _contains), "not": (1, _not)}


class _Steps:
    """What one evaluation may still spend (see `MAX_STEPS`)."""

    __slots__ = ("left",)

    def __init__(self) -> None:
        self.left = MAX_STEPS


def _arithmetic(op: str, a, b):
    """`a op b` for two numbers. Division by zero is null; an answer that is too large (or not a number) is an error."""
    try:
        if op == "+":
            result = a + b
        elif op == "-":
            result = a - b
        elif op == "*":
            result = a * b
        elif b == 0:
            return None
        else:
            result = a / b
    except OverflowError:  # a whole number too large to turn into a decimal one
        raise ConditionError("a number in the condition is too large to work with") from None
    if isinstance(result, float):
        if not math.isfinite(result):
            raise ConditionError("a number in the condition is too large to work with")
    elif result.bit_length() > MAX_RESULT_BITS:
        raise ConditionError("a number in the condition grows too large to work with")
    return result


def _walk(node, scope: dict, reads: dict | None, steps: _Steps):
    steps.left -= 1
    if steps.left < 0:
        raise ConditionError("the condition is too big to work out over this data")
    if isinstance(node, Lit):
        return node.value
    if isinstance(node, ListLit):
        return [_walk(i, scope, reads, steps) for i in node.items]
    if isinstance(node, Path):
        if node.root in scope:
            value, tracked = scope[node.root], False
        else:
            value, tracked = scope["$data"].get(node.root), True
        for seg in node.segs:
            value = value.get(seg) if isinstance(value, dict) else None
        if tracked and reads is not None:
            reads[node.text] = value
        return value
    if isinstance(node, Neg):
        v = _walk(node.operand, scope, reads, steps)
        return -v if _is_num(v) else None
    if isinstance(node, Call):
        return FUNCTIONS[node.name][1](*[_walk(a, scope, reads, steps) for a in node.args])
    if isinstance(node, Quant):
        items = _walk(node.source, scope, reads, steps)
        if not isinstance(items, list):
            return None
        results = [_walk(node.cond, {**scope, node.var: item}, reads, steps) for item in items]
        results = [_bool(r) for r in results]
        if node.kind == "every":
            if any(r is False for r in results):
                return False
            return True if all(r is True for r in results) else None
        if any(r is True for r in results):
            return True
        return False if all(r is False for r in results) else None
    if not isinstance(node, Bin):
        raise ConditionError("that is not a condition read by this module")
    op = node.op
    if op == "and":
        return _and(_walk(node.left, scope, reads, steps), _walk(node.right, scope, reads, steps))
    if op == "or":
        return _or(_walk(node.left, scope, reads, steps), _walk(node.right, scope, reads, steps))
    a = _walk(node.left, scope, reads, steps)
    b = _walk(node.right, scope, reads, steps)
    if op == "=":
        return _eq(a, b)
    if op == "!=":
        return not _eq(a, b)
    if op == "in":
        if isinstance(b, list):
            return any(_eq(a, x) for x in b)
        return None if b is None else _eq(a, b)
    if op in ("<", "<=", ">", ">="):
        if (_is_num(a) and _is_num(b)) or (isinstance(a, str) and isinstance(b, str)):
            return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        return None
    if op == "+" and isinstance(a, str) and isinstance(b, str):
        return a + b
    if _is_num(a) and _is_num(b):
        return _arithmetic(op, a, b)
    return None


def evaluate(tree, data: dict, reads: dict | None = None):
    """The value of a parsed expression over `data`. `reads`, when given, collects every data path read and
    the value found (what a decision's history records). Raises `ConditionError` (and nothing else) when it can't be
    worked out: a number too large to work with, or more work than `MAX_STEPS`."""
    try:
        return _walk(tree, {"$data": data if isinstance(data, dict) else {}}, reads, _Steps())
    except RecursionError:  # data nested thousands deep; the condition's own depth is capped when it is read
        raise ConditionError("the data is nested too deeply for the condition to read") from None


def _say(value) -> str:
    """A value in the words a household reads: numbers and text as written, a list or record by what it is."""
    if isinstance(value, str):
        return '"' + (value if len(value) <= 40 else value[:37] + "...") + '"'
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a group of fields"
    return str(value)


def evaluate_condition(tree, data: dict) -> tuple[bool, dict]:
    """(does it hold, the data it read). A null answer is "no"; an answer that is neither true, false nor
    null is an error: a condition must be a yes/no question."""
    reads: dict = {}
    value = evaluate(tree, data, reads)
    if value is None:
        return False, reads
    if not isinstance(value, bool):
        raise ConditionError(f"the condition answered {_say(value)}, not true or false")
    return value, reads


# --- checking against the data model ----------------------------------------------------------------------

def _infer_literal(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if _is_num(value):
        return "number"
    return "string" if isinstance(value, str) else "any"


def analyze(tree, model: DataModel) -> list[str]:
    """Problems with a condition against the data model: a field the model doesn't declare, a value outside a
    field's declared choices, a condition that isn't a yes/no question. Empty means it is sound."""
    problems: list[str] = []

    def full_path(node: Path, scope: dict) -> str | None:
        """The declared path a Path reads, or None for an item of something with no declared shape."""
        if node.root in scope:
            base = scope[node.root]
            if base is None:
                return None
            return base + "".join(f".{s}" for s in node.segs)
        return node.text

    def visit(node, scope: dict) -> str:
        if isinstance(node, Lit):
            return _infer_literal(node.value)
        if isinstance(node, ListLit):
            for item in node.items:
                visit(item, scope)
            return "list"
        if isinstance(node, Path):
            path = full_path(node, scope)
            if path is None:
                return "any"
            field = model.resolve(path)
            if field is None:
                shown = node.text
                problems.append(f"it reads '{shown}', which the data model doesn't declare")
                return "any"
            return field.type
        if isinstance(node, Neg):
            visit(node.operand, scope)
            return "number"
        if isinstance(node, Call):
            kinds = [visit(a, scope) for a in node.args]
            if node.name == "not":
                _want_bool(kinds[0], "not()")
                return "boolean"
            if node.name == "count":
                if kinds[0] not in ("list", "any"):
                    problems.append("count() needs a list")
                return "number"
            return "boolean"
        if isinstance(node, Quant):
            source_path = None
            if isinstance(node.source, Path):
                source_path = full_path(node.source, scope)
            kind = visit(node.source, scope)
            if kind not in ("list", "any"):
                problems.append(f"'{node.kind} {node.var} in ...' needs a list")
            inner = {**scope, node.var: (source_path + "[]") if source_path else None}
            result = visit(node.cond, inner)
            _want_bool(result, f"the '{node.kind}' condition")
            return "boolean"
        # Bin
        if node.op in ("and", "or"):
            _want_bool(visit(node.left, scope), node.op)
            _want_bool(visit(node.right, scope), node.op)
            return "boolean"
        left = visit(node.left, scope)
        visit(node.right, scope)
        if node.op in ("=", "!=", "in"):
            _check_choice(node, scope)
            return "boolean"
        if node.op in ("<", "<=", ">", ">="):
            return "boolean"
        return "string" if node.op == "+" and left == "string" else "number"

    def _want_bool(kind: str, where: str) -> None:
        if kind not in ("boolean", "any"):
            problems.append(f"{where} needs a true/false value, not a {kind}")

    def _check_choice(node: Bin, scope: dict) -> None:
        """`path = "x"` where the field lists its allowed values and "x" isn't one of them."""
        sides = [(node.left, node.right)]
        if node.op != "in":
            sides.append((node.right, node.left))
        for field_side, value_side in sides:
            if not isinstance(field_side, Path):
                continue
            path = full_path(field_side, scope)
            field = model.resolve(path) if path else None
            if not field or not field.enum:
                continue
            literals = []
            if isinstance(value_side, Lit) and isinstance(value_side.value, str):
                literals = [value_side.value]
            elif isinstance(value_side, ListLit):
                literals = [i.value for i in value_side.items if isinstance(i, Lit) and isinstance(i.value, str)]
            for lit in literals:
                if lit not in field.enum:
                    problems.append(f"'{field_side.text}' is never \"{lit}\"; its choices are {', '.join(field.enum)}")

    try:
        top = visit(tree, {})
    except RecursionError:  # a tree not made by `parse`, which caps its depth
        raise ConditionError("the condition is nested too deeply to check") from None
    if top not in ("boolean", "any"):
        problems.append(f"it is {_a(top)}, not a true/false condition")
    return problems


def _a(word: str) -> str:
    return f"{'an' if word[:1].lower() in tuple('aeiou') else 'a'} {word}"


def reads_of(tree) -> list[str]:
    """Every data path (not loop variables) the condition reads, in order, without repeats."""
    found: list[str] = []

    def visit(node, local: frozenset) -> None:
        if isinstance(node, Path):
            if node.root not in local and node.text not in found:
                found.append(node.text)
        elif isinstance(node, ListLit):
            for item in node.items:
                visit(item, local)
        elif isinstance(node, Neg):
            visit(node.operand, local)
        elif isinstance(node, Call):
            for arg in node.args:
                visit(arg, local)
        elif isinstance(node, Quant):
            visit(node.source, local)
            visit(node.cond, local | {node.var})
        elif isinstance(node, Bin):
            visit(node.left, local)
            visit(node.right, local)

    visit(tree, frozenset())
    return found


# --- do the ways out cover every case? --------------------------------------------------------------------

def _always_yes_or_no(node) -> bool:
    """Is this condition true or false for every possible data, never null? A comparison with `<` or `>`, a bare
    field, `contains` and `count` all answer null when the field is missing (or isn't the kind of thing compared),
    and null is "neither", so none of them is. `=`, `!=` and `x in [...]` always answer, and `and`, `or` and `not` of
    conditions that do."""
    if isinstance(node, Lit):
        return isinstance(node.value, bool)
    if isinstance(node, Bin):
        if node.op in ("=", "!="):
            return True
        if node.op == "in":
            return isinstance(node.right, ListLit)
        if node.op in ("and", "or"):
            return _always_yes_or_no(node.left) and _always_yes_or_no(node.right)
        return False
    if isinstance(node, Call):
        return node.name == "not" and _always_yes_or_no(node.args[0])
    return False


def _same(a, b) -> bool:
    """The same tree. `==` on trees can't tell the number 1 from `true` (`1 == True` in Python), and `x = 1` and
    `x != true` are not opposites, so compare how they are written."""
    return repr(a) == repr(b)


def _opposites(a, b) -> bool:
    """Exactly one of these holds for every possible data."""
    if isinstance(b, Call) and b.name == "not" and _always_yes_or_no(a) and _same(b.args[0], a):
        return True
    return (isinstance(a, Bin) and isinstance(b, Bin) and {a.op, b.op} == {"=", "!="}
            and _same(a.left, b.left) and _same(a.right, b.right))


def covers_every_case(trees: list, model: DataModel | None = None) -> bool:
    """Can we tell, from how they are written, that at least one of these conditions holds for every possible data,
    including data where a field has no value yet? Only these shapes are recognised: the condition `true`; a
    condition and its `not(...)`, when the condition always answers yes or no; and `x = v` with `x != v`. Anything else
    needs a default way out, because anything else can answer null, and null holds nowhere. That includes
    `n < 5` with `n >= 5`, a yes/no field against `true` and `false`, and every choice of a field with a list of
    choices: each would leave the work with no way out the day the field is empty. (`model` is not used any more; it is
    kept so callers needn't change.)"""
    try:
        if any(isinstance(t, Lit) and t.value is True for t in trees):
            return True
        return any(_opposites(a, b) or _opposites(b, a) for i, a in enumerate(trees) for b in trees[i + 1:])
    except RecursionError:  # a tree not made by `parse`, which caps its depth: not provably covering
        return False


__all__ = [
    "Bin", "Call", "Lit", "ListLit", "Neg", "Path", "Quant", "analyze", "covers_every_case", "evaluate",
    "evaluate_condition", "parse", "parent_path", "reads_of", "tree_depth",
]
