"""The FEEL subset conditions are written in: reading, running (deterministic, no eval), and checking against the
data model (path 15.3)."""

import pytest

from process_engine import feel
from process_engine.datamodel import DataModel
from process_engine.errors import ConditionError, ProcessLoadError

MODEL = DataModel.from_dict({
    "plan": "object",
    "plan.items": "list",
    "plan.items[].name": "string",
    "plan.items[].risk": {"type": "string", "enum": ["low", "medium", "high"]},
    "plan.items[].systems": "list",
    "plan.items[].systems[]": "string",
    "plan.total": "number",
    "approved": "boolean",
    "mode": {"type": "string", "enum": ["a", "b", "c"]},
    "payload": "any",
})

DATA = {
    "plan": {"items": [
        {"name": "rename a file", "risk": "low", "systems": ["files"]},
        {"name": "delete the old backup", "risk": "medium", "systems": ["backup", "files"]},
    ], "total": 2},
    "approved": True,
    "mode": "a",
    "result": {"verdict": "ok"},
}


def run(text: str, data: dict | None = None):
    return feel.evaluate(feel.parse(text), DATA if data is None else data)


@pytest.mark.parametrize("text, expected", [
    ("1 + 2 * 3 = 7", True),
    ("(1 + 2) * 3 = 9", True),
    ("10 / 4 = 2.5", True),
    ("-3 < 0", True),
    ('"a" + "b" = "ab"', True),
    ("'single quotes' = \"single quotes\"", True),
    ("true and not(false)", True),
    ("false or false", False),
    ("null = null", True),
    ("plan.total >= 2 and plan.total < 3", True),
    ("plan.total != 2", False),
    ("count(plan.items) = 2", True),
    ('contains("delete the backup", "delete")', True),
    ('contains(plan.items, "x")', None),
    ('result.verdict in ["ok", "fine"]', True),
    ('result.verdict in ["bad"]', False),
    ('"files" in plan.items', False),
    ('some i in plan.items satisfies i.risk = "medium"', True),
    ('every i in plan.items satisfies i.risk = "low"', False),
    ('every i in plan.items satisfies i.risk in ["low", "medium"]', True),
    ('some i in plan.items satisfies contains(i.name, "delete")', True),
    ('some i in plan.items satisfies some s in i.systems satisfies s = "backup"', True),
    ('every i in plan.items satisfies count(i.systems) > 0', True),
    ("every i in [] satisfies false", True),
    ("some i in [] satisfies true", False),
    ('some i in plan.items satisfies i.risk = "high" or contains(i.name, "delete")', True),
    ("= approved = true", True),
])
def test_conditions_evaluate(text, expected):
    assert run(text) == expected


def test_missing_fields_read_as_null_and_a_null_condition_is_no():
    assert run("plan.nothing") is None
    assert run("plan.nothing < 3") is None
    holds, _ = feel.evaluate_condition(feel.parse("plan.nothing < 3"), DATA)
    assert holds is False
    # three-valued logic: a null doesn't hide a decided side
    assert run("false and plan.nothing < 3") is False
    assert run("true or plan.nothing < 3") is True
    assert run("true and plan.nothing < 3") is None


def test_booleans_are_not_numbers():
    assert run("true = 1") is False
    assert run("false = 0") is False
    assert run("1 = 1.0") is True


def test_a_condition_must_answer_yes_or_no():
    with pytest.raises(ConditionError, match="not true or false"):
        feel.evaluate_condition(feel.parse("plan.total"), DATA)


def test_reads_are_recorded_with_the_values_found():
    reads: dict = {}
    feel.evaluate(feel.parse('approved = true and result.verdict = "ok"'), DATA, reads)
    assert reads == {"approved": True, "result.verdict": "ok"}
    # loop variables are not data reads
    reads = {}
    feel.evaluate(feel.parse('some i in plan.items satisfies i.risk = "low"'), DATA, reads)
    assert set(reads) == {"plan.items"}
    assert feel.reads_of(feel.parse('some i in plan.items satisfies i.risk = "low" and approved')) == [
        "plan.items", "approved"]


@pytest.mark.parametrize("text", [
    "__import__('os').system('x')",
    "plan.items[0]",
    "open(\"f\")",
    "lambda: 1",
    "plan.total ** 2",
    "1 +",
    "count(1, 2)",
    "(1",
    "every x satisfies true",
    "some in plan.items satisfies true",
    "unknown_function(1)",
    "",
    "a ; b",
    "if true then 1 else 2",
])
def test_things_that_are_not_conditions_are_refused_at_reading(text):
    with pytest.raises(ConditionError):
        feel.parse(text)


def test_a_huge_or_deeply_nested_condition_is_refused():
    with pytest.raises(ConditionError, match="longer"):
        feel.parse("1 = 1 and " * 400 + "true")
    with pytest.raises(ConditionError, match="nested"):
        feel.parse("(" * 60 + "true" + ")" * 60)


def test_evaluation_is_deterministic():
    tree = feel.parse('some i in plan.items satisfies i.risk = "medium"')
    assert {feel.evaluate(tree, DATA) for _ in range(5)} == {True}


# --- against the data model ---

@pytest.mark.parametrize("text", [
    'approved = true',
    'plan.total > 1',
    'some i in plan.items satisfies i.risk = "high"',
    'every i in plan.items satisfies contains(i.name, "x")',
    'some i in plan.items satisfies some s in i.systems satisfies s = "a"',
    'payload.anything.at.all = 1',
    'result.verdict = "ok"',
    'error.code = "x"',
    'mode in ["a", "b"]',
    "not(approved)",
])
def test_conditions_over_declared_fields_are_sound(text):
    assert feel.analyze(feel.parse(text), MODEL) == []


@pytest.mark.parametrize("text, fragment", [
    ("plan.cost > 3", "plan.cost"),
    ("goal.name = 1", "goal.name"),
    ('some i in plan.items satisfies i.colour = "red"', "i.colour"),
    ('every i in plan.items satisfies i.risk = "extreme"', 'never "extreme"'),
    ('mode = "z"', 'never "z"'),
    ("plan.total", "not a true/false"),
    ("plan.total and approved", "needs a true/false"),
    ("count(approved) = 1", "needs a list"),
    ("some i in approved satisfies true", "needs a list"),
    ("not(plan.total)", "needs a true/false"),
])
def test_conditions_the_data_model_does_not_support_are_refused(text, fragment):
    problems = feel.analyze(feel.parse(text), MODEL)
    assert problems and any(fragment in p for p in problems), problems


def test_data_model_refuses_bad_declarations():
    with pytest.raises(ProcessLoadError, match="not a field path"):
        DataModel.from_dict({"plan items": "list"})
    with pytest.raises(ProcessLoadError, match="type 'text'"):
        DataModel.from_dict({"a": "text"})
    with pytest.raises(ProcessLoadError, match="which is a string"):
        DataModel.from_dict({"a": "string", "a.b": "string"})
    with pytest.raises(ProcessLoadError, match="twice"):
        DataModel([*DataModel.from_dict({"a": "string"}).declared(), *DataModel.from_dict({"a": "string"}).declared()])


def test_data_model_lists_its_fields_for_the_page():
    listing = MODEL.listing()
    risk = next(f for f in listing if f["path"] == "plan.items[].risk")
    assert risk["enum"] == ["low", "medium", "high"]
    assert {"result", "error", "error.code"} <= {f["path"] for f in listing}


# --- do the ways out cover every case? ---

def covers(*texts: str) -> bool:
    return feel.covers_every_case([feel.parse(t) for t in texts], MODEL)


def test_exhaustive_conditions_are_recognised():
    assert covers("true")
    assert covers("plan.total = 5", "plan.total != 5")
    assert covers('mode != "a"', 'mode = "a"')
    assert covers('approved = true', 'not(approved = true)')
    assert covers('mode in ["a", "b"]', 'not(mode in ["a", "b"])')
    assert covers('mode = "a" or approved = true', 'not(mode = "a" or approved = true)')
    assert covers("false", "not(false)")


def test_conditions_that_may_leave_a_gap_are_not_called_exhaustive():
    assert not covers("plan.total < 5", "plan.total > 5")
    assert not covers("mode = \"a\"", "mode = \"b\"")
    assert not covers("approved")
    assert not covers("plan.total < 5", "plan.total >= 6")
    assert not covers()


def test_conditions_that_leave_out_a_field_with_no_value_are_not_called_exhaustive():
    """The ordering comparisons, a yes/no field against true and false, and every choice of a field are exhaustive on
    paper. Run over data with the field missing, each answers null, which holds nowhere, so none of them is accepted without a default."""
    for texts in [
        ("plan.total < 5", "plan.total >= 5"), ("plan.total > 5", "plan.total <= 5"),
        ("approved", "not(approved)"), ("approved = true", "approved = false"),
        ('mode = "a"', 'mode = "b"', 'mode = "c"'), ('mode in ["a", "b"]', 'mode = "c"'),
        ('contains(mode, "a")', 'not(contains(mode, "a"))'),
    ]:
        assert not covers(*texts), texts
        holds = [feel.evaluate_condition(feel.parse(t), {})[0] for t in texts]  # the field is missing
        assert not any(holds), texts
    for texts in [("plan.total = 5", "plan.total != 5"), ('mode in ["a"]', 'not(mode in ["a"])')]:
        assert covers(*texts), texts
        assert any(feel.evaluate_condition(feel.parse(t), {})[0] for t in texts), texts


def test_a_number_and_true_are_told_apart_when_looking_for_opposites():
    assert not covers("mode = 1", "mode != true")  # `1 == True` in Python, but these are not opposites
