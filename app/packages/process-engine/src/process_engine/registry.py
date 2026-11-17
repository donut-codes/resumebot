"""Steps bind to code by name (requirement 15.2).

- The **action registry**: a service task names an action (`porter:action="staff.assign"`); the registry holds
  the async function and says what it reads, what it returns, what it does in plain words, and where it lives.
  `listing()` is what the How they work page shows. Registering a name twice is an error.
- The **catalog**: the names a file may use for roles, instruction files, response templates and card kinds. A
  file that names one that isn't there fails to load.

Actions are the only place the app's safeguards live (approval for changes to a household system, logins never
reaching a model, the fence around tools, separation of duties, the Inbox limit, the context budget). A process
routes work between actions; it can't remove a safeguard because none is in the process.
"""

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from process_engine.config import config, names_of

#: `async def action(session, instance, data) -> dict`. `data` is a copy of the instance's data.
ActionFn = Callable[..., Awaitable[dict | None]]

_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class RegistryError(ValueError):
    """An action or card kind couldn't be registered."""


@dataclass(frozen=True)
class ActionSpec:
    name: str
    fn: ActionFn
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    description: str
    source: str


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ActionSpec] = {}

    def register(self, name: str, fn: ActionFn, inputs: list[str] | tuple[str, ...] = (),
                 outputs: list[str] | tuple[str, ...] = (), description: str = "", source: str = "") -> ActionSpec:
        if not _NAME.match(name or ""):
            raise RegistryError(f"'{name}' is not an action name (lower-case words joined by dots, e.g. staff.assign)")
        for what, names in (("inputs", inputs), ("outputs", outputs)):
            if isinstance(names, str):  # a string is a sequence of letters: ("a",) was meant, "a" was written
                raise RegistryError(f"the action '{name}': {what} should be a list of names, not the text '{names}'; "
                                    f"write ['{names}']")
        if name in self._actions:
            raise RegistryError(f"the action '{name}' is already registered")
        if not inspect.iscoroutinefunction(fn):
            raise RegistryError(f"the action '{name}' must be an async function (session, instance, data)")
        if not description.strip():
            raise RegistryError(f"the action '{name}' needs a plain description; the How they work page shows it")
        if not source.strip():
            raise RegistryError(f"the action '{name}' needs its source (where the code lives)")
        spec = ActionSpec(name, fn, tuple(inputs), tuple(outputs), description.strip(), source.strip())
        self._actions[name] = spec
        return spec

    def get(self, name: str) -> ActionSpec | None:
        return self._actions.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._actions

    def names(self) -> list[str]:
        return sorted(self._actions)

    def listing(self) -> list[dict]:
        """For the How they work page: name, description, inputs, outputs, source."""
        return [
            {"name": s.name, "description": s.description, "inputs": list(s.inputs), "outputs": list(s.outputs),
             "source": s.source}
            for s in sorted(self._actions.values(), key=lambda s: s.name)
        ]


#: The registry the app's own code registers its actions in. Tests make their own `ActionRegistry()`.
actions = ActionRegistry()


def register_action(name: str, fn: ActionFn, inputs: list[str] | tuple[str, ...] = (),
                    outputs: list[str] | tuple[str, ...] = (), description: str = "", source: str = "") -> ActionSpec:
    """Register `fn` as the action `name` in the default registry (see `ActionRegistry.register`)."""
    return actions.register(name, fn, inputs, outputs, description, source)


def list_actions() -> list[dict]:
    return actions.listing()


# --- card kinds -------------------------------------------------------------------------------------------

_CARD_KINDS: dict[str, str] = {}


def register_card_kind(name: str, description: str = "") -> None:
    """A user task names a card kind (what the app shows the person: a card, a form, a question). The app can also
    list its kinds with `config.card_kinds`; registering one of those again is an error."""
    if not _NAME.match(name or ""):
        raise RegistryError(f"'{name}' is not a card kind name")
    if name in _CARD_KINDS or name in names_of(config.card_kinds):
        raise RegistryError(f"the card kind '{name}' is already registered")
    _CARD_KINDS[name] = description


# --- what a file may name ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Catalog:
    """The names a process file may use. `roles` and `cards` are what the app configured (`config.roles`,
    `config.card_kinds`, `register_card_kind`); `instructions` and `templates` are the files its
    `config.instructions` source lists."""

    roles: frozenset[str] = field(default_factory=frozenset)
    cards: frozenset[str] = field(default_factory=frozenset)
    instructions: frozenset[str] = field(default_factory=frozenset)
    templates: frozenset[str] = field(default_factory=frozenset)

    def has_instructions(self, ref: str) -> bool:
        return self.short("instructions", ref) in self.instructions

    def has_template(self, ref: str) -> bool:
        return self.short("templates", ref) in self.templates

    def short(self, kind: str, ref: str) -> str:
        return config.instructions.short_name(kind, ref)


def default_catalog() -> Catalog:
    """The names that exist now: what the app configured (roles, instruction and template files, card kinds) and the
    card kinds registered."""
    return Catalog(
        roles=names_of(config.roles),
        cards=frozenset(_CARD_KINDS) | names_of(config.card_kinds),
        instructions=frozenset(config.instructions.names("instructions")),
        templates=frozenset(config.instructions.names("templates")),
    )
