"""The owner's two keys for the process engine.

The engine keeps its rows (a process's versions, a run's owner) under an id for the owner and an id for the person who
made a change. This app has one of each, fixed from the owner's name (`settings.owner`); no address on the API carries
them. There is no sign-in: the app is reached on the owner's own network.
"""

import uuid

from app.settings import settings

_NAMESPACE = uuid.UUID("5a0fb6c6-0c43-4d52-9d83-3b1f1c35a0c1")


def entity_id() -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"entity:{settings.owner}")


def member_id() -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"member:{settings.owner}")
