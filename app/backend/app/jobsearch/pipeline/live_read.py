"""Browser live messages: the shape of each one, made from the fields of a row, and (`build`) the message of a change
built from the database by whoever was told of it.

Two paths make a message and both call the shape functions below, so the two cannot drift: `live._collect` (inside the
flush that made the change, from the fields the session holds) and `build` (afterwards, from the rows).

The tags of an item, a tuple of short strings (never row data), say what the change was:
- `new`: the row was made (a role: `from` is None and `changed` is ["new"]; a notification: `was_read` is None).
- `deleted`: the row was removed; the message is `{id, deleted: true}` (task, notification, employer, site).
- `was:<status>`: a role's or a packet's status before the change (`from`).
- `changed:status`, `changed:favorite`: what changed on a role (`changed`, in that order).
- `was_read:yes`, `was_read:no`: whether a notification had been read before the change (`was_read`).
- an area (`jobs`, `gaps`, `agents`, `missions`, `harness`, `prep`, `go`, `ready`; `AREAS`): on a `changed` item, the
  screen the row is behind. The item's id names the row, or what names it to the screen; the message has no data."""

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.models import Employer, Opportunity, Packet, ProfileVersion
from app.jobsearch.profile.base import KINDS as PROFILE_KINDS
from app.models.work import Notification, Task
from app.sites.models import Site

#: The board's key for the closed lane (`board.CLOSED`).
CLOSED_LANE = "closed"
#: The columns of a site a `site` message carries: the plain ones its row reads straight from the table.
SITE_FIELDS = ("url", "label", "on", "list_name", "read_by")


def lane_of(status: str | None) -> str | None:
    """The board lane a status is shown in: its own, or Closed for every closed status."""
    if status is None:
        return None
    return CLOSED_LANE if status in stages.CLOSED else status


def iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def role_data(state: dict, was: str | None, changed: list[str]) -> dict:
    """A `role` message from what the session holds of the role (`state`: its loaded attributes)."""
    from app.jobsearch.pipeline import board  # the board reads much of the job search

    status = state.get("status")
    closed = status in stages.CLOSED
    return {
        "id": str(state.get("id")), "title": state.get("title"), "company": state.get("company"),
        "from": was, "to": status, "lane_from": lane_of(was), "lane_to": lane_of(status),
        "label": stages.LABELS.get(status, status) if status else None,
        "closed_reason": board.closed_reason_key(status, state.get("ended_reason")) if closed else None,
        "reason": state.get("screened_out_reason") if status == stages.GATED_OUT else None,
        "favorite": state.get("favorited_at") is not None,
        "changed": changed,
    }


def packet_step(status: str | None, brief: Any) -> dict:
    """A packet's progress (`packets.progress_of`) from its status and its brief."""
    from app.jobsearch.writing import packets  # the packets read much of the job search

    found = brief.get(packets.STEP_KEY) if isinstance(brief, dict) else None
    return packets.progress_of(status or "", found if isinstance(found, str) else None)


def packet_data(state: dict, was: str | None) -> dict:
    """A `packet` message from the packet's fields (`state`) and its status before (`was`, None for a new one)."""
    return {"id": str(state.get("id")), "role_id": str(state.get("role_id")), "version": state.get("version"),
            "status": state.get("status"), "from": was,
            "progress": packet_step(state.get("status"), state.get("brief"))}


def notification_data(source: Any, was_read: bool | None) -> dict:
    """A `notification` message: the notification as the inbox shows it (`source` has its attributes), with whether it
    had been read before the change (`was_read`, None for a new one)."""
    from app.inbox import service as inbox  # the inbox reads the trail and the actors

    return {**inbox.notification_view(source), "was_read": was_read}


def employer_whole(source: Any) -> dict:
    """An `employer` message whole: the employer's row on Employers (`employers.row`) with `fetched_at`."""
    from app.jobsearch.employers import core as employers  # it reads much of the job search

    return {**employers.row(source), "fetched_at": iso(source.fetched_at)}


def profile_version_data(state: dict) -> dict:
    """A `profile_version` message from the version's fields (`state`)."""
    return {"kind": state.get("kind"), "version": state.get("version"), "saved_by": state.get("saved_by"),
            "why": state.get("why") or "", "saved_at": iso(state.get("saved_at"))}


def site_data(held: dict) -> dict:
    """A `site` message: its id and the plain columns `held` has (`SITE_FIELDS`)."""
    out: dict[str, Any] = {"id": str(held.get("id"))}
    for name in SITE_FIELDS:
        if name in held:
            out["list" if name == "list_name" else name] = held[name]
    return out


#: The kind of the message that tells a screen its own data changed: `{"area": ..., "ids": [...]}`, no data in it.
CHANGED = "changed"
#: The areas a `changed` message names, in the order the messages of one batch are made. Each is a screen (or a few)
#: that reads one endpoint and used to ask it again and again; `live.AREA_WATCH` says which tables are behind which.
AREAS = ("jobs", "gaps", "agents", "missions", "harness", "prep", "go", "ready")


def changed_messages(ids: Sequence[str], tags: Sequence[tuple[str, ...]]) -> list[dict]:
    """The `changed` messages of a run of items: one for each area the tags name (`tags[i]` are the tags of `ids[i]`,
    the area each), in the order of `AREAS`, with the ids of that area once each in the order they came. A tag that is
    no area makes nothing. No row is read: the screen reads its own endpoint again."""
    by_area: dict[str, list[str]] = {}
    for one, mine in zip(ids, tags, strict=True):
        for area in mine:
            if area in AREAS and one not in by_area.setdefault(area, []):
                by_area[area].append(one)
    return [{"area": area, "ids": by_area[area]} for area in AREAS if area in by_area]


#: The messages a browser stream can ask for (`live.TOPICS`; a test keeps the two equal).
KINDS = frozenset({"changed", "role", "counts", "packet", "summary", "task", "notification", "employer",
                    "profile_version",
                    "site"})
#: The kinds whose message for a removed row is `{id, deleted: true}`, built from the id and the tag `deleted` alone.
DELETABLE = frozenset({"task", "notification", "employer", "site"})
#: The table each kind that is built from a row reads.
_MODELS: dict[str, Any] = {"role": Opportunity, "packet": Packet, "task": Task, "notification": Notification,
                           "employer": Employer, "profile_version": ProfileVersion, "site": Site}

ROLE_FIELDS = ("id", "title", "company", "status", "ended_reason", "screened_out_reason", "favorited_at")
PACKET_FIELDS = ("id", "role_id", "version", "status", "brief")
VERSION_FIELDS = ("kind", "version", "saved_by", "why", "saved_at")


def _was(tags: tuple[str, ...]) -> str | None:
    """The status before, from the tag `was:<status>`."""
    return next((tag[4:] for tag in tags if tag.startswith("was:")), None)


def _role_message(row: Any, tags: tuple[str, ...]) -> dict | None:
    state = {name: getattr(row, name) for name in ROLE_FIELDS}
    if "new" in tags:
        return role_data(state, None, ["new"])
    changed = [name for name in ("status", "favorite") if f"changed:{name}" in tags]
    return role_data(state, _was(tags) if "changed:status" in tags else state["status"], changed)


def _packet_message(row: Any, tags: tuple[str, ...]) -> dict | None:
    state = {name: getattr(row, name) for name in PACKET_FIELDS}
    return packet_data(state, None if "new" in tags else (_was(tags) or state["status"]))


def _task_message(row: Any, tags: tuple[str, ...]) -> dict | None:
    from app.inbox import service as inbox  # the inbox reads the trail and the actors

    return inbox.task_view(row)


def _notification_message(row: Any, tags: tuple[str, ...]) -> dict | None:
    if "new" in tags:
        was_read = None
    elif "was_read:yes" in tags:
        was_read = True
    elif "was_read:no" in tags:
        was_read = False
    else:
        was_read = row.read_at is not None
    return notification_data(row, was_read)


def _employer_message(row: Any, tags: tuple[str, ...]) -> dict | None:
    return employer_whole(row)


def _profile_version_message(row: Any, tags: tuple[str, ...]) -> dict | None:
    if row.kind not in PROFILE_KINDS:
        return None
    return profile_version_data({name: getattr(row, name) for name in VERSION_FIELDS})


def _site_message(row: Any, tags: tuple[str, ...]) -> dict | None:
    return site_data({name: getattr(row, name) for name in ("id", *SITE_FIELDS)})


#: The function that makes the message of one row, by kind.
SHAPES: dict[str, Callable[[Any, tuple[str, ...]], dict | None]] = {
    "role": _role_message, "packet": _packet_message, "task": _task_message, "notification": _notification_message,
    "employer": _employer_message, "profile_version": _profile_version_message, "site": _site_message}


async def _rows(session: AsyncSession, model: Any, owner: str, ids: Sequence[str]) -> dict[str, Any]:
    """The owner's rows of `model` with these ids, by id as text. No query when there are none."""
    if not ids:
        return {}
    wanted = {uuid.UUID(one) for one in ids}
    found = await session.scalars(select(model).where(model.owner == owner, model.id.in_(wanted)))
    return {str(row.id): row for row in found}


async def build(session: AsyncSession, owner: str, kind: str, ids: Sequence[str],
                tags: Sequence[tuple[str, ...]]) -> list[dict]:
    """The browser messages of a change, built from the rows by whoever was told of it: one for each id, in the order
    given (`tags[i]` are the tags of `ids[i]`, see the top of this module). A row that is gone, or is another owner's,
    makes no message. `counts` takes no ids and gives the owner's counts. Pass a reader's session
    (`db.read_session_factory`): this reads, it never writes."""
    if kind not in KINDS:
        raise ValueError(f"no such kind: {kind}")
    if len(ids) != len(tags):
        raise ValueError("one tags tuple for each id")
    if kind == "counts":
        from app.jobsearch.pipeline import live  # live imports this module at its top, so this one waits

        return [await live.counts(session, owner)]
    if kind == "summary":
        return [{"id": one} for one in ids]
    if kind == CHANGED:
        return changed_messages(ids, tags)
    gone = kind in DELETABLE
    rows = await _rows(session, _MODELS[kind], owner,
                       [one for one, mine in zip(ids, tags, strict=True) if not (gone and "deleted" in mine)])
    out: list[dict] = []
    for one, mine in zip(ids, tags, strict=True):
        if gone and "deleted" in mine:
            out.append({"id": one, "deleted": True})
            continue
        row = rows.get(str(uuid.UUID(one)))
        message = None if row is None else SHAPES[kind](row, mine)
        if message is not None:
            out.append(message)
    return out
