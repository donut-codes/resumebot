"""One key of a JSON column, as SQL: what the role's research says under a key, whether a task holds a key.

The columns are `sa.JSON` and the database is SQLite. SQLAlchemy's
own accessor (`column["key"].as_string()`) sends the path as an argument (`json_extract(col, ?)`), and SQLite's planner
will not match a partial or expression index whose text carries the path (`json_extract(gate_facts, '$.state') =
'pending'`) to a statement that carries it as an argument: it scans the table (`EXPLAIN QUERY PLAN`,
`tests/core/test_sqlite_queries.py`). So keys made of word characters, which is every key the code asks for, are written
into the statement as the index writes them, `'$.key'`; any other key goes through the accessor, which quotes it.
"""

import re

from sqlalchemy import String, func, literal_column
from sqlalchemy.sql.elements import ColumnElement

#: The keys that are written into the statement as they are: nothing in them can end a quoted path or mean anything
#: in one.
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def field(column, *keys: str) -> ColumnElement[str]:
    """What the JSON object in `column` holds under `keys` (one key, or the keys of the objects inside it), as SQL text;
    null when it is not there, a step of the way is not an object, or it is JSON `null`. A number comes as the number,
    `true` and `false` as 1 and 0, an object or a list as its JSON text with no spaces."""
    if all(_PLAIN.match(key) for key in keys):
        return func.json_extract(column, literal_column("'$." + ".".join(keys) + "'"), type_=String)
    return (column[keys[0]] if len(keys) == 1 else column[keys]).as_string()


def has(column, key: str) -> ColumnElement[bool]:
    """Whether the JSON object in `column` holds `key` with something other than JSON `null` (`has_key`, for a key whose
    `null` nobody reads as an answer)."""
    return field(column, key).is_not(None)
