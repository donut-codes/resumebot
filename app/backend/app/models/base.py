import uuid
from datetime import datetime

from process_engine.types import UtcDateTime
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.timeutil import utc_now


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utc_now, onupdate=utc_now
    )
