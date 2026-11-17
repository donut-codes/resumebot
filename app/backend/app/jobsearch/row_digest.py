"""A fingerprint of a set of rows that combines: how many there are, and two 64-bit hashes of each row's text,
exclusive-or-ed together. The same rows give the same fingerprint in any order, a row added, removed or changed changes
it, and no row can be told from another by its place.

It is the key of what is built from rows (the index of roles, the gate rules), so what is kept is returned only while
the rows are the ones it was made from. SQLite has no hash function and no `bit_xor`, so the database gives each row's
text (`row_text`, one statement over the rows) and the hashes are made here (`over`); the rows are read as text and
nothing is built from them.
"""

import hashlib
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Text, func


def row_text(*columns: Any) -> Any:
    """A row's text, as the database writes it: a JSON array of its values, so nothing in it can be confused with
    another row's (a missing value is `null`, not an empty string)."""
    return func.json_array(*columns, type_=Text)


def over(texts: Iterable[str]) -> tuple[int, int, int]:
    """How many rows there are and the two hashes (the halves of a 128-bit `blake2b` of each row's text),
    exclusive-or-ed together."""
    n = a = b = 0
    for text in texts:
        hashed = hashlib.blake2b(text.encode(), digest_size=16).digest()
        n, a, b = n + 1, a ^ int.from_bytes(hashed[:8]), b ^ int.from_bytes(hashed[8:])
    return n, a, b
