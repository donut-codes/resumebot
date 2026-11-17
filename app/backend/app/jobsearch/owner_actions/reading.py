"""The owner's door, the part that reads (docs/PRD.md path 50.6): the list of the actions and the run of a read action.
It is the part of the door the page program serves, so it imports nothing of the engine: `runner.py` (jobs, dry runs and
the tasks that do a write) is the engine's and stays out of the page."""

import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFound
from app.jobsearch.owner_actions.registry import REGISTRY, Action, Args, BadArguments, Ctx


class UnknownAction(NotFound):
    pass


def plain(value: Any) -> Any:
    """`value` as the JSON it is stored and answered as."""
    return json.loads(json.dumps(value, default=str))


def action_named(name: str, *, writes: bool) -> Action:
    action = REGISTRY.get(name)
    if action is None:
        raise UnknownAction(f"there is no action {name}: actions.list names them")
    if action.writes != writes:
        raise BadArguments(f"{name} {'writes: use act.do' if action.writes else 'only reads: use act.read'}")
    return action


def check_args(action: Action, raw: dict[str, Any] | None) -> Args:
    try:
        return action.inputs.model_validate(raw or {})
    except ValidationError as exc:
        said = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
        raise BadArguments(f"{action.name} was sent arguments it does not take: {said}") from None


def listing() -> dict[str, Any]:
    """`actions.list`: every action, from the registry."""
    return {"ok": True, "actions": [a.listed() for a in REGISTRY.values()],
            "how": "act.read(action, args) for the reads; act.do(action, args, dry_run) for the writes (dry_run: true "
                   "changes nothing and says what a real call would do); act.job(id) for a job still running"}


async def read(session: AsyncSession, owner: str, name: str, raw: dict[str, Any] | None) -> dict[str, Any]:
    """Run a read action. Whatever the services did while reading is rolled back: a read changes nothing."""
    action = action_named(name, writes=False)
    args = check_args(action, raw)
    try:
        outcome = await action.run(Ctx(session, owner), args)
        return {"ok": True, "action": name, "result": plain(outcome.result)}
    finally:
        await session.rollback()
