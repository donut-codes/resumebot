"""The package's own declarative base and column mixins.

The engine's tables live on `Base.metadata`, a metadata of the package's own, so the package never needs the app's
`Base`. The app includes these tables in its migrations (see the README, "Tables"): either by listing
`process_engine.base.Base.metadata` beside its own as Alembic's `target_metadata`, or by copying the tables into its own
metadata with `Table.to_metadata` (which lets a host add its own foreign keys).

No table here has a foreign key to a table the app owns: the owner (`entity_id`) and the people (`changed_by`,
`signed_off_by`) are plain UUID columns. An app that wants the database to enforce them adds the constraint in its own
migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from process_engine.types import UtcDateTime, utc_now


class Base(DeclarativeBase):
    pass


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, onupdate=utc_now
    )
