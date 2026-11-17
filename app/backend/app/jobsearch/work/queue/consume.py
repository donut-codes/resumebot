"""What the dispatcher takes to-dos of.

The dispatcher (`loop.py`) is started with the app and always runs. Each name here is one kind of work it does from
to-dos; `TOPIC_OF`, `OWN_TOPIC_OF`, `TURN_TOPICS` and `WALK_TOPICS` say which topics each takes:

- `ticks`: the small housekeeping parts (`housekeeping.MINUTE`, `housekeeping.HOUR`) run from the minute and hour
  ticks;
- `sift`: the cheap filter runs from `posting.sift`;
- `roles`: postings become roles from `posting.roles`;
- `details`: the descriptions roles lack are read from `role.details`;
- `turn`: the pipeline's turn runs from the to-dos of its steps (`TURN_TOPICS`); the steps that ask for a
  turn post them, and Launch's wait is a wait for them;
- `pull`: starting the next job for a free pool session (`session_kinds.pull`) runs from `pool.owed`;
- `browser`: making the browser's queue match the roles and sites (`browser.queue.refresh`) runs from
  `browser.reconcile`, and the lease expiry on the minute tick;
- `walk`: the runner dispatcher's pass (`dispatcher.dispatch_pass`) runs from the `ready.<kind>` to-dos, from
  a lane's own `dispatch.idle` and on the minute tick.
- `asks` (the split): a question the page put to the engine (`engine_calls.ask`) is answered from its `ask` to-do
  (`handlers.engine_ask`, `engine_asks`).

`consumed_topics` is what the janitor leaves to the dispatcher; `wake`, `listen` and `forget` are how a commit that
added a to-do tells the loop to look at once. Pure of the app: only the topics and the settings."""

import asyncio

from app.jobsearch.work.queue import topics

#: The names of the kinds of work the dispatcher does from to-dos.
TICKS, SIFT, ROLES, DETAILS, TURN = "ticks", "sift", "roles", "details", "turn"
PULL, BROWSER, WALK, ASKS = "pull", "browser", "walk", "asks"
NAMES = (TICKS, SIFT, ROLES, DETAILS, TURN, PULL, BROWSER, WALK, ASKS)

#: The topic a name's handler takes: the cheap filter, the making of roles and the read of descriptions.
TOPIC_OF = {SIFT: topics.POSTING_SIFT, ROLES: topics.POSTING_ROLES, DETAILS: topics.ROLE_DETAILS}
#: The topic of the names that move no step of the turn: the pool's pull, the browser's queue and the page's asks.
OWN_TOPIC_OF = {PULL: topics.POOL_OWED, BROWSER: topics.BROWSER_RECONCILE, ASKS: topics.ENGINE_ASK}

#: The topics of the turn's own steps, which one handler takes together and runs the whole turn for (whole form: every
#: key is the whole owner): the roles' steps (`role.settle`), the deck (`deck.dirty`), the employer limit and the
#: research of the employers of the deck (`pipeline.deck._stages`).
TURN_TOPICS = (topics.ROLE_SETTLE, topics.DECK_DIRTY, topics.EMPLOYER_LIMIT, topics.EMPLOYER_RESEARCH)

#: The topics of the walk: work of a kind of session is waiting (`ready.<kind>`), or a lane has gone idle
#: (`dispatch.idle`: since without it a lane freed while work waited was commanded at
#: the next minute's tick, where the 10 second loop it replaces did it within ten seconds). One handler takes all of
#: them and runs the dispatcher's pass once (whole form: it asks every kind and every idle lane, as the pass did).
WALK_TOPICS = (*(topics.ready(kind) for kind in topics.READY_KINDS), topics.DISPATCH_IDLE)

#: The events of the running dispatchers: set by a commit that added a to-do (`wake`).
_waiters: set[asyncio.Event] = set()


def consumed_topics() -> frozenset[str]:
    """The topics the dispatcher takes to-dos of: the janitor leaves these to it."""
    return frozenset((*topics.CLOCK, *TOPIC_OF.values(), *OWN_TOPIC_OF.values(), *TURN_TOPICS, *WALK_TOPICS))


def wake() -> None:
    """A transaction that added a to-do has committed: look now rather than at the next idle poll. A hint; the row is
    the record, and the dispatcher looks at least every 30 seconds."""
    for event in list(_waiters):
        event.set()


def listen(event: asyncio.Event) -> None:
    _waiters.add(event)


def forget(event: asyncio.Event) -> None:
    _waiters.discard(event)
