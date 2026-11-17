"""The extension points: everything the engine asks the app that embeds it.

The package imports nothing from an app. Where it needs something only the app knows (which roles an agent step may
name, which instruction files exist, who owns an instance, where the shipped process files live), it asks the one
`Config` object here, which the app fills once at start-up with `configure(...)`:

- `roles`: the roles an agent step (`porter:role`) may name. Default: none, so every agent step is refused.
- `card_kinds`: the card kinds a user step (`porter:card`) may name, besides `register_card_kind`. Default: none.
- `instructions`: an `InstructionSource` for the instruction and template files a step may name. Default: none exist.
- `owner_key`: the key of an instance's data holding the id of the owner it runs for. Default: `"owner"`.
- `owner_candidates`: `(data, subject) -> strings` that may be the owner's id. Default: `data[owner_key]`.
- `shipped_dir`: the folder of the shipped `*.bpmn` files, for versions and rollback. Default: nothing is shipped.
- `register_processes`: imports whatever registers the actions the shipped files name. Default: does nothing.
- `member_names`: `async (session, owner_id) -> {member_id: name}`, who made a version. Default: no names.
- `shipped_label`: the history line for version 0, the shipped file. Default: `"The file as it ships."`.
- `known_attributes`: the names in the file's `porter:` namespace that the app reads itself (the engine ignores them),
  besides `engine`, `harness` and `calls`, which are always accepted. Default: `None`, which refuses only a name that
  looks like a misspelling of one the engine reads (`porter:acton`, so a typo never quietly drops a safeguard). Give the
  names and every other `porter:` name is refused as well.
- `on_drive_failure`: `async (session, entity_id, key, what, exc, subject=None)`, called when the live work of
  `live.attempt` fails. The savepoint is already rolled back, so the work is undone and the run is held where it was;
  nothing is cancelled, and the app says what happened to the person (the hook is the app's one place for that).
  `subject` is what the caller said the attempt was about (`attempt(..., subject=)`), else `None`. Default: `None`,
  which only logs the failure.
- `on_drive_success`: `async (session, entity_id, key, what, subject=None)`, called after a live `attempt` finished its
  work, so an app that raised something for a failure can close it. Default: `None`.

`roles`, `card_kinds` and `known_attributes` are sets or lists of names, never one bare string (that would be read
letter by letter, so `configure` refuses it). `roles` and `card_kinds` may be given as a function, called each time a
catalog is built, so an app can read them from a table that isn't ready when it configures. Everything else an app plugs
in goes through the registries it already has (`registry.register_action`, `live.register_drivable`).

Configuring twice replaces the earlier values field by field (only the fields named change); `reset()` puts every
default back (for tests). Read the live values as `config.config.<field>`: `configure` changes that one object.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession


@runtime_checkable
class InstructionSource(Protocol):
    """Where a step's instruction and response-template files come from. `kind` is `"instructions"` or `"templates"`."""

    def names(self, kind: str) -> Iterable[str]:
        """Every file name of that kind there is (without folder or extension)."""
        ...

    def short_name(self, kind: str, ref: str) -> str:
        """What a process file stores (`instructions/summary.write.md`, a bare name, ...) as a name."""
        ...


class NoInstructions:
    """The default: no instruction or template files exist, so a step naming one fails to load."""

    def names(self, kind: str) -> Iterable[str]:
        return ()

    def short_name(self, kind: str, ref: str) -> str:
        return os.path.basename(str(ref or "").strip().replace("\\", "/"))


RoleSource = Iterable[str] | Callable[[], Iterable[str]]
#: `async (session, entity_id, key, what, exc, subject=None) -> None`: see `Config.on_drive_failure`.
DriveFailureHook = Callable[..., Awaitable[None]]
#: `async (session, entity_id, key, what, subject=None) -> None`: see `Config.on_drive_success`.
DriveSuccessHook = Callable[..., Awaitable[None]]


def _default_owner_candidates_for(key: str) -> Callable[[dict | None, str | None], Iterable[str]]:
    def candidates(data: dict | None, subject: str | None) -> Iterable[str]:
        return [str(data[key])] if data and data.get(key) else []

    return candidates


@dataclass
class Config:
    roles: RoleSource = ()
    card_kinds: RoleSource = ()
    instructions: InstructionSource = field(default_factory=NoInstructions)
    owner_key: str = "owner"
    owner_candidates: Callable[[dict | None, str | None], Iterable[str]] | None = None
    shipped_dir: Callable[[], Path | str | None] | None = None
    register_processes: Callable[[], None] | None = None
    member_names: Callable[[AsyncSession, uuid.UUID], Awaitable[dict[uuid.UUID, str]]] | None = None
    shipped_label: str = "The file as it ships."
    known_attributes: Iterable[str] | None = None
    on_drive_failure: DriveFailureHook | None = None
    on_drive_success: DriveSuccessHook | None = None

    def candidates(self, data: dict | None, subject: str | None) -> list[str]:
        """The strings that may be the id of the owner an instance runs for."""
        hook = self.owner_candidates or _default_owner_candidates_for(self.owner_key)
        return list(hook(data, subject))


#: The one config. Modules read it as `config.config.<field>`, and `configure` changes it in place, so every reader
#: sees the change.
config = Config()


def configure(**values: Any) -> Config:
    """Set the named fields (see the module's table); the rest keep their values. Returns the config."""
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(values) - known)
    if unknown:
        raise TypeError(
            f"process_engine has no setting {', '.join(unknown)}; the settings are {', '.join(sorted(known))}"
        )
    for name in _NAME_SETS:
        _refuse_bare_text(name, values.get(name))
    for name, value in values.items():
        setattr(config, name, value)
    return config


def reset() -> Config:
    """Every field back to its default."""
    fresh = Config()
    for f in fields(Config):
        setattr(config, f.name, getattr(fresh, f.name))
    return config


#: The settings that are a set or list of names, where one bare string is a slip (a string is a sequence of letters).
_NAME_SETS = ("roles", "card_kinds", "known_attributes")


def _refuse_bare_text(name: str, value: Any) -> None:
    if isinstance(value, str):
        raise TypeError(f"{name} should be a set or a list of names, not the text '{value}' "
                        f"(it would be read one letter at a time); write ['{value}']")


def names_of(source: RoleSource) -> frozenset[str]:
    """A `roles` or `card_kinds` value as a set: called when it is a function. A bare string is refused."""
    names = source() if callable(source) else source
    _refuse_bare_text("roles and card kinds", names)
    return frozenset(names)


__all__ = [
    "Config", "DriveFailureHook", "DriveSuccessHook", "InstructionSource", "NoInstructions", "config", "configure",
    "names_of", "reset",
]
