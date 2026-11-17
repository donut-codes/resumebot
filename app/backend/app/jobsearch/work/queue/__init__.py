"""The change-driven work queue: the code that makes a change writes a small to-do in
the same transaction, one dispatcher (a later phase) hands the to-dos out, and an hourly sweep (later) is the net.

The pieces: the table (`models.Todo`), `post` from every writer (`watch.py`'s watch list for every ordinary save,
an explicit `post` beside each direct statement), the tripwire and the coverage comparison that prove nothing is
missed, and the hour's janitor. `loop.py`'s dispatcher reads the due
to-dos (`service.due`, `done`, `fail`) and runs `handlers.py`'s handlers for the cheap filter (`posting.sift`), the
making of roles (`posting.roles`), the read of descriptions (`role.details`), two ticks that carry six small
housekeeping parts, the pipeline's whole turn (`role.settle`, `deck.dirty`, `employer.limit`,
`employer.research`), the pool's pull (`pool.owed`), the browser's queue
(`browser.reconcile`) and the runner dispatcher's pass (`ready.<kind>`); `consume.py` says which topics the dispatcher
takes to-dos of. No setting is added.

Writers use `queue.post(...)` and `queue.exempt(reason)`. The hook and the tripwire are listeners on the session and are
registered when the database is configured (`app.db.configure`), not by importing this package."""

from app.jobsearch.work.queue.service import (
    MissedPost,
    exempt,
    exempted,
    post,
    post_all,
    post_claimed,
    post_new_roles,
    post_roles,
    suppressing,
)
from app.jobsearch.work.queue.topics import (
    ANY,
    BROWSER_RECONCILE,
    DECK_DIRTY,
    DISPATCH_IDLE,
    EMPLOYER_LIMIT,
    EMPLOYER_RESEARCH,
    POOL_OWED,
    POSTING_ROLES,
    POSTING_SIFT,
    ROLE_DETAILS,
    ROLE_SETTLE,
    RULES_CHANGED,
    TICK_HOUR,
    TICK_MINUTE,
    browser_ready,
    ready,
)

__all__ = [
    "ANY", "BROWSER_RECONCILE", "DECK_DIRTY", "DISPATCH_IDLE", "EMPLOYER_LIMIT", "EMPLOYER_RESEARCH", "POOL_OWED",
    "POSTING_ROLES", "POSTING_SIFT", "ROLE_DETAILS", "ROLE_SETTLE", "RULES_CHANGED", "MissedPost", "exempt",
    "TICK_HOUR", "TICK_MINUTE", "browser_ready", "exempted", "post", "post_all", "post_claimed",
    "post_new_roles", "post_roles", "ready", "suppressing",
]
