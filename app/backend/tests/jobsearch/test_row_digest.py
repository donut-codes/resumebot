"""The fingerprint of a set of rows (`row_digest`) that the index of roles and the gate rules are kept under: it is made
here, over the text SQLite gives for each row, because SQLite has no hash function and no `bit_xor`. Synthetic data."""

from sqlalchemy import literal, select

from app import db
from app.jobsearch import row_digest


def test_the_same_rows_give_the_same_digest_in_any_order():
    rows = ['["a", "b"]', '["c", null]', '["d", ""]']
    assert row_digest.over(rows) == row_digest.over(reversed(rows))


def test_a_row_added_removed_or_changed_changes_the_digest():
    rows = ['["a", "b"]', '["c", null]']
    digest = row_digest.over(rows)
    assert row_digest.over([*rows, '["d", ""]']) != digest
    assert row_digest.over(rows[:1]) != digest
    assert row_digest.over(['["a", "b"]', '["c", "x"]']) != digest
    assert row_digest.over([]) == (0, 0, 0)


def test_two_equal_rows_do_not_cancel_each_other():
    """An exclusive-or of the same hash twice is nothing: the count is what tells two equal rows from none."""
    assert row_digest.over(['["a"]', '["a"]']) != row_digest.over([])


async def test_a_missing_value_is_not_an_empty_one_in_the_text_of_a_row(js):
    async with db.session_factory()() as session:
        missing = await session.scalar(select(row_digest.row_text(literal("a"), literal(None))))
        empty = await session.scalar(select(row_digest.row_text(literal("a"), literal(""))))
    assert missing != empty
