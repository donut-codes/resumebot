"""A condition that is too big, too deep, too slow or too large in its numbers is a plain `ConditionError`, whatever
it is, and nothing else ever escapes `parse`, `analyze`, `evaluate` or `evaluate_condition`."""

import random

import pytest

from process_engine import feel
from process_engine.datamodel import DataModel
from process_engine.errors import ConditionError

MODEL = DataModel.from_dict({
    "plan": "object", "plan.items": "list", "plan.items[].name": "string",
    "plan.items[].risk": {"type": "string", "enum": ["low", "medium", "high"]},
    "plan.items[].systems": "list", "plan.items[].systems[]": "string",
    "plan.total": "number", "approved": "boolean", "mode": {"type": "string", "enum": ["a", "b", "c"]},
    "payload": "any", "n": "number", "items": "list",
})

DATA = {
    "plan": {"items": [
        {"name": "rename a file", "risk": "low", "systems": ["files"]},
        {"name": "delete the old backup", "risk": "medium", "systems": ["backup", "files"]},
    ], "total": 2},
    "approved": True, "mode": "a", "n": 7, "items": list(range(100)), "result": {"verdict": "ok"},
}


def everything(text: str):
    """Read, check and run a condition: the whole of what the engine does with one."""
    tree = feel.parse(text)
    feel.analyze(tree, MODEL)
    feel.evaluate(tree, DATA)
    return feel.evaluate_condition(tree, DATA)


def refused(text: str, fragment: str) -> None:
    with pytest.raises(ConditionError, match=fragment):
        everything(text)


# --- depth ---

@pytest.mark.parametrize("text", [
    "-" * 1900 + "1 = 1",
    "-" * 101 + "1 = 1",
    "1" + " + 1" * 999 + " = 1",
    "1" + " + 1" * 400 + " = 1",
    "true" + " and true" * 300,
    "true" + " or true" * 450,
    "1" + " * 2" * 600 + " = 1",
    "not(" * 41 + "true" + ")" * 41,
    "[" * 41 + "1" + "]" * 41,
    "(" * 41 + "true" + ")" * 41,
    "-" * 40 + "(" * 40 + "1" + ")" * 40 + " = 1",
])
def test_a_deep_or_long_chain_is_refused_not_a_crash(text):
    with pytest.raises(ConditionError, match="nested|longer"):
        feel.parse(text)


def test_a_chain_just_inside_the_limit_still_reads_and_runs():
    text = "true" + " and true" * 90  # 91 terms: a left-leaning tree 91 deep
    tree = feel.parse(text)
    assert feel.tree_depth(tree) == 91
    assert feel.evaluate(tree, {}) is True
    assert feel.evaluate(feel.parse("-" * 20 + "5 = 5"), {}) is True


def test_a_tree_made_by_hand_and_far_deeper_than_reading_allows_is_still_a_plain_error():
    tree = feel.Lit(1)
    for _ in range(5000):
        tree = feel.Neg(tree)
    assert feel.tree_depth(tree) > feel.MAX_TREE_DEPTH
    for run in (lambda: feel.evaluate(tree, {}), lambda: feel.analyze(tree, MODEL)):
        try:
            run()
        except ConditionError:
            pass  # a plain refusal is fine; so is an answer; a RecursionError is what must not escape


def test_every_nested_in_every_has_a_limit():
    three = ("every a in items satisfies every b in items satisfies every c in items satisfies true")
    assert feel.parse(three)
    four = "every a in items satisfies " + three
    with pytest.raises(ConditionError, match="inside one another only 3 deep"):
        feel.parse(four)
    deep_source = "every a in " * 200 + "items satisfies true"
    with pytest.raises(ConditionError):
        feel.parse(deep_source)


# --- work ---

def test_nested_every_over_a_long_list_runs_out_of_steps_instead_of_hanging():
    # 100 x 100 x 100 = a million evaluations; the budget is 50,000
    text = "every a in items satisfies every b in items satisfies every c in items satisfies a + b + c >= 0"
    tree = feel.parse(text)
    with pytest.raises(ConditionError, match="too big to work out"):
        feel.evaluate(tree, DATA)


def test_an_ordinary_condition_over_an_ordinary_list_is_well_inside_the_budget():
    text = "some i in plan.items satisfies some s in i.systems satisfies s = \"backup\""
    assert feel.evaluate(feel.parse(text), DATA) is True
    big = {"items": [{"x": k, "ys": [1, 2, 3]} for k in range(500)]}
    tree = feel.parse("every i in items satisfies some y in i.ys satisfies y = 2 and i.x >= 0")
    assert feel.evaluate(tree, big) is True


# --- numbers ---

def test_a_number_with_too_many_digits_is_refused_at_reading():
    refused("1" + "0" * 399 + " / 1 = 1", "more than 18 digits")
    refused("1" * 19 + " = 1", "more than 18 digits")
    assert feel.evaluate(feel.parse("123456789012345678 > 1"), {}) is True


def test_arithmetic_that_cannot_be_answered_is_an_error_not_null():
    """Chosen: a plain `ConditionError`. A null would make the condition quietly "no" for a figure it couldn't read, and
    the work would go the wrong way without a word; an error fails the step and says why."""
    huge = {"n": 10 ** 400}
    with pytest.raises(ConditionError, match="too large"):
        feel.evaluate(feel.parse("n / 1"), huge)
    with pytest.raises(ConditionError, match="too large"):
        feel.evaluate(feel.parse("n + 1.5"), huge)
    with pytest.raises(ConditionError, match="too large"):
        feel.evaluate(feel.parse("n * 1.5"), huge)
    with pytest.raises(ConditionError, match="grows too large"):
        feel.evaluate(feel.parse("n * n * n"), {"n": 2 ** 100})
    with pytest.raises(ConditionError, match="too large"):
        feel.evaluate_condition(feel.parse("n * 10.0 > 1"), {"n": 1.7e308})
    # an ordinary figure is still a figure, and dividing by zero is still null
    assert feel.evaluate(feel.parse("n * 2 + 1"), {"n": 10}) == 21
    assert feel.evaluate(feel.parse("n / 0"), {"n": 10}) is None
    assert feel.evaluate(feel.parse("n / 4"), {"n": 10}) == 2.5


def test_comparing_a_huge_number_is_exact_and_needs_no_conversion():
    assert feel.evaluate(feel.parse("n > 1.5"), {"n": 10 ** 400}) is True
    assert feel.evaluate(feel.parse("n = n"), {"n": 10 ** 400}) is True


def test_data_that_is_not_a_record_reads_as_nothing_found():
    assert feel.evaluate(feel.parse("n = null"), None) is True
    assert feel.evaluate(feel.parse("n = null"), "oops") is True


def test_the_answer_is_said_in_plain_words():
    for text, shown in [("n", "7"), ("mode", '"a"'), ("plan.items", "a list"), ("plan", "a group of fields"),
                        ("n + 0.5", "7.5")]:
        with pytest.raises(ConditionError) as caught:
            feel.evaluate_condition(feel.parse(text), DATA)
        assert f"answered {shown}, not true or false" in str(caught.value)
        assert "'" not in str(caught.value).split("answered")[1]


# --- strings ---

def test_backslash_n_in_a_string_is_a_newline_and_the_rest_stand_for_themselves():
    assert feel.evaluate(feel.parse(r'"a\nb" = "a" + "\n" + "b"'), {}) is True
    assert feel.evaluate(feel.parse(r'"a\tb"'), {}) == "a\tb"
    assert feel.evaluate(feel.parse(r'"a\rb"'), {}) == "a\rb"
    assert feel.evaluate(feel.parse(r'"say \"hi\""'), {}) == 'say "hi"'
    assert feel.evaluate(feel.parse(r"'it\'s'"), {}) == "it's"
    assert feel.evaluate(feel.parse(r'"back\\slash"'), {}) == "back\\slash"
    assert feel.evaluate(feel.parse(r'contains("line one\nline two", "\n")'), {}) is True


def test_digits_are_the_ascii_ones():
    with pytest.raises(ConditionError, match="can't read"):
        feel.parse("\u0663 = 3")  # an Arabic-Indic digit three


# --- only ConditionError ever escapes: a fuzz ---

PIECES = [
    "1", "2.5", "0", "99999999999999999999", "-", "+", "*", "/", "(", ")", "[", "]", ",", ".", "=", "!=", "<", "<=",
    ">", ">=", "and", "or", "not", "in", "every", "some", "satisfies", "true", "false", "null", "count", "contains",
    "x", "n", "items", "plan.items", "a.b.c", "i", '"s"', "'t'", '"a\\nb"', "\\", '"', "'", "\u00e9", "\x00", "\u0663",
    "\n", "\t", "=", "e", "1e5", "..", "**", "%", "{", "}", ";", "@", "$data", "_",
]

SEEDS = [
    'some i in plan.items satisfies i.risk = "medium"', "count(plan.items) = 2 and approved",
    "n / 0 > 1 or not(approved)",
    'every i in plan.items satisfies some s in i.systems satisfies contains(s, "f")', 'mode in ["a", "b"]',
    "-n * (2 + n) <= 3", '= approved = true and result.verdict = "ok"',
]


def _random_condition(rng: random.Random) -> str:
    kind = rng.randrange(4)
    if kind == 0:  # soup
        return " ".join(rng.choice(PIECES) for _ in range(rng.randrange(1, 30)))
    if kind == 1:  # a good condition with a few characters changed
        text = list(rng.choice(SEEDS))
        for _ in range(rng.randrange(1, 4)):
            at = rng.randrange(len(text) + 1)
            op = rng.randrange(3)
            if op == 0 and text:
                del text[min(at, len(text) - 1)]
            elif op == 1:
                text.insert(at, rng.choice("()[]\"'\\-+*/=<>,. \n9x\x00\u00e9"))
            elif text:
                text[min(at, len(text) - 1)] = rng.choice("()[]\"'\\-+*/=<>,. \n9x\x00\u00e9")
        return "".join(text)
    if kind == 2:  # a good condition repeated or wrapped, to reach the limits
        seed = rng.choice(SEEDS)
        joiner = rng.choice([" and ", " or ", " + ", " - ", " * ", " / ", " = ", " in "])
        return joiner.join([seed] * rng.randrange(1, 40))
    glue = rng.choice(["not(", "-", "(", "[", "count(", "every x in ", "some x in items satisfies ", "- ("])
    return glue * rng.randrange(1, 120) + rng.choice(["true", "1", "x", "items"])


ODD_DATA = {"n": 10 ** 400, "items": [None, 1, "x", [], {}], "plan": "text", "approved": [1]}


def _attempt(call, *args) -> None:
    """Run it; a `ConditionError` is a fair answer, anything else is the failure this test exists to catch."""
    try:
        call(*args)
    except ConditionError:
        pass


def _use_every_way(tree) -> None:
    """Everything the engine does with a condition once it is read."""
    _attempt(feel.analyze, tree, MODEL)
    _attempt(feel.reads_of, tree)
    _attempt(feel.covers_every_case, [tree, tree], MODEL)
    for data in (DATA, {}, ODD_DATA):
        _attempt(feel.evaluate, tree, data)
        _attempt(feel.evaluate_condition, tree, data)


def test_nothing_but_condition_error_escapes_a_fuzz():
    rng = random.Random(20261008)
    readable = 0
    for _ in range(4000):
        text = _random_condition(rng)
        try:
            tree = feel.parse(text)
        except ConditionError:
            continue
        readable += 1
        _use_every_way(tree)
    assert readable > 400  # the fuzz reaches the evaluator, not only the reader


def test_a_condition_that_is_not_text_is_a_plain_error():
    for value in (None, 5, b"x", ["a"], {"a": 1}):
        with pytest.raises(ConditionError):
            feel.parse(value)
