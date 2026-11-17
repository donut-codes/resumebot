"""The database base. The job-search app has its own engine and sessions (`app.db`); this holds the
declarative base the process engine's tables sit on (copied from another project's code (app/PROVENANCE.md); its Clock
pool and the default engine are not needed here)."""

from collections.abc import Iterable

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def one_of(table: str, column: str, values: Iterable[str]) -> CheckConstraint:
    """The database's check that a text column holds one of a closed list of words. The StrEnum that names the
    words once makes the list, so what the code may store and what the database accepts cannot drift apart."""
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=f"ck_{table}_{column}")
