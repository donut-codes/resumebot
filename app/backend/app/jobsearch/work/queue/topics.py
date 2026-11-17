"""The kinds of look a to-do asks for. A topic says what to look at, a key where.

Pure data: no import of the app, so any module can name a topic without a cycle."""

#: A key that means "anything of this topic may have changed": the handler runs its whole-owner path.
ANY = "*"

ROLE_SETTLE = "role.settle"
POSTING_SIFT = "posting.sift"
POSTING_ROLES = "posting.roles"
ROLE_DETAILS = "role.details"
DECK_DIRTY = "deck.dirty"
EMPLOYER_LIMIT = "employer.limit"
EMPLOYER_RESEARCH = "employer.research"
RULES_CHANGED = "rules.changed"
BROWSER_RECONCILE = "browser.reconcile"
POOL_OWED = "pool.owed"
DISPATCH_IDLE = "dispatch.idle"
#: A question the page put to the engine (`engine_calls.ask`): the key is the ask's id, and `*` is the start's and
#: the hour's look at every ask that has no answer. No save of a row posts it, so it is not in `WRITE_DRIVEN`.
ENGINE_ASK = "ask"

#: The clock's own to-dos: two persisted ticks that post themselves again with a due time (`loop.py`). No writer posts
#: them; they are what notices a change only time makes, and carry the small housekeeping that left the 10 second loop.
TICK_MINUTE = "tick.minute"
TICK_HOUR = "tick.hour"
CLOCK = (TICK_MINUTE, TICK_HOUR)

#: The two causes (`Todo.cause`) the dispatcher itself reads; every other cause is for the log and the coverage
#: comparison.
#: A person or a step is waiting for a turn (`ask`: Launch, a report that ranked roles, a step that made roles), so the
#: turn is not made to wait out its pause; and the hour tick's own look at what only time changes (`clock`: the first 72
#: hours, the stale days, research grown old), so what that look moves is not a writer's miss.
ASK = "ask"
CLOCK_CAUSE = "clock"

#: `ready.<kind>`: work of a kind of session (`work.session_kinds`) is waiting. The kinds are written out so this module
#: stays free of imports; a test pins them to the kinds the walk knows.
READY_KINDS = ("parser", "checker", "gaps", "search", "titles", "analysis", "researcher", "site", "seat", "coach",
               "mail", "browser_reader", "pathfinder")


def ready(kind: str) -> str:
    """The topic for work waiting for a session of `kind`."""
    return f"ready.{kind}"


def browser_ready(kind: str) -> str | None:
    """The topic for a browser item of `kind` that is waiting: a session maps a site (`map`), another reads (`source`,
    `read`, `scan`); the app's own code works a `record` item and a `listing` item (a board role's page), so no
    session is waiting for either."""
    if kind == "map":
        return ready("pathfinder")
    return None if kind in ("record", "listing") else ready("browser_reader")


#: Every topic a writer can post, by name (the ready topics each their own).
WRITE_DRIVEN = (ROLE_SETTLE, POSTING_SIFT, POSTING_ROLES, ROLE_DETAILS, DECK_DIRTY, EMPLOYER_LIMIT, EMPLOYER_RESEARCH,
                RULES_CHANGED, BROWSER_RECONCILE, POOL_OWED, DISPATCH_IDLE, *(ready(k) for k in READY_KINDS))
