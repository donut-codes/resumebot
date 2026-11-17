"""The cheap filter (docs/PRD.md 28.10, 30.1, 31.1): code, per owner, over postings the index has seen but nobody has
judged, cheapest first, so tokens are spent only on what code cannot settle.

1. The gates that need no judgement (a named or gated employer, a link to a site the owner avoids, a place the owner
   doesn't take, pay under the floor where the posting shows pay, an old posting when age is a gate, an employer fact
   the record holds): the one gate check (`gating/check.py`, docs/PRD.md 39.11). A posting that fails one is stopped
   with the
   gate. A place that says nothing (empty, or only a count such as "26 Locations") is
   unknown and stops nothing; where the posting's job system names the places on the posting's own page
   that page is read first, so the place is judged on the places and not on their count.
2. The classifier (docs/PRD.md 29.6), trained on the owner's own roles. It ranks every posting that passed: its
   probability is kept, for the order the title check and the readers take them in, and for fit's "unknown" ask. It
   stops none: whatever its rating, a posting goes on to the title check. With no model yet (the cold start) nothing
   is rated.

Every posting that is not stopped becomes a role (`pipeline/from_posting.py`): the title pass is retired, and the Haiku
rank orders the roles. The classifier never sends a posting to a reader and never stops one.

A posting that is stopped keeps the rule that stopped it as data (the step, the gate, the line it is, and the versions
of What to look for and Hard pass in force: `gating.posting_stop_values`, docs/PRD.md 39.12), shown on the Pipeline's
Gated stage, and never makes a role. Nothing here judges fit or level: that is the readers' and the scoring's work.
The owner's search phrases are not a filter: they are for sites that need a typed search.

**It writes in slices, and judges before it locks.** The ids of the postings that wait are read once (the run's own
list: a read that keeps adding postings cannot lengthen it), and the postings are read, judged in code and written
`CHUNK` at a time, each slice committed (`checkpoint.reached` where a driver saves at safe points, else
`db.release_if_free`), so the night's filter, which judged every new posting in one transaction, holds no posting for
more than a slice. A slice is read and its transaction ended at once (`db.release_if_free`: on SQLite a read in a
writer session takes the one writer turn), so it is judged with no turn held and no attribute set on a row (the loop
given back between postings); only then is it locked (`as_read.lock`, a plain read: the
transaction that writes holds the database, so nothing changes a row before the write) and looked at again: a posting
filtered since it was read (another run was first) is left as that run judged it. The rest of the slice is the verdicts
written to the rows still open, a few statements. A filter run `only` for the postings the owner just added is one slice
in the caller's transaction, as it was."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import as_read, off_turn
from app.jobsearch.fetch.polite import Fetcher
from app.jobsearch.fit import classifier
from app.jobsearch.gating import check as gating
from app.jobsearch.judged import ROLES as ROUTE_ROLES
from app.jobsearch.models import JobPosting
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import same_role
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.profile.schema import Profile
from app.jobsearch.work import checkpoint
from app.timeutil import owner_today

#: How many postings one transaction of the filter writes.
CHUNK = 60


def rank(model: classifier.Model | None, posting: JobPosting) -> tuple[str, float | None]:
    """What the classifier does with a posting that passed the gates (docs/PRD.md 31.1): it rates it. Returns (why,
    probability): the probability is kept on the posting, to put the likeliest first, and `why` says what put it there.
    It stops nothing and routes nothing: every posting that passed goes to the title check, whatever its rating. With
    no model yet there is no rating. The rating and its words come from one reading of the posting's text."""
    if model is None:
        return "no history to learn from yet", None
    prob, words = model.rate(classifier.posting_text(posting))
    return f"ranked {prob:.2f}" + (f": {', '.join(words)}" if words else ""), prob


def waiting_where(owner: str) -> tuple:
    """What makes a posting wait for the filter: open, nobody has filtered it, and it is no role's. `sift` reads exactly
    these, and so does the queue's probe (`work/queue/handlers.py`): the partial index
    `ix_js_postings_owner_unfiltered` (migration 0141) holds only them."""
    return (JobPosting.owner == owner, JobPosting.is_open, JobPosting.filtered_at.is_(None),
            JobPosting.opportunity_id.is_(None))


async def sift(session: AsyncSession, owner: str, profile: Profile, now: datetime | None = None,
               fetch: Fetcher | None = None, reads: int = 0, only: Collection[uuid.UUID] | None = None) -> dict:
    """Filter every open posting nobody has filtered yet. Returns the candidates (postings that passed, each with why
    and its route), how many each rule stopped and how many went each route.

    A posting that is a role the owner already has, in any writing of its company and title or by its link
    (`same_role`), is linked to that role and judged no further ("already a role").

    A posting that passes the gates is routed to become a role, set on it as `route` `roles`
    (`role_from_posting.make_roles`), whatever the classifier thinks of it; the classifier's probability is kept.
    Anything the gates stop keeps its reason as data: the step, the gate, the line it is, and the versions of What to
    look for and Hard pass in force when it was stopped.

    A place that says nothing (empty, or only a count such as "26 Locations") is unknown and stops nothing. With the
    work location gate on, `fetch` set and `reads` not 0, the postings whose job system names the places on the
    posting's own page have that page read first, at most `reads` of them (counted
    in the night's descriptions, `read` in the result; a negative number is no limit), so the gate judges the places
    themselves. A page that can't be read, or that does not name every place the list counted, leaves the place
    unknown and the posting goes on.

    `only` narrows the filter to those postings (a job the owner just added by its link, `add_link.py`, is put through
    the same code gates at once, so it shows where it stands); nothing else changes."""
    now = now or datetime.now(UTC)
    slices = only is None  # the owner's own add is one slice in the caller's transaction
    query = select(JobPosting.id).where(*waiting_where(owner)).order_by(JobPosting.first_seen, JobPosting.id)
    if only is not None:
        query = query.where(JobPosting.id.in_(list(only)))
    # The rules and the owner's roles are plain data read whole, with no writer's turn where the session is free
    # (`off_turn.reading`): each posting is asked about in any writing (`same_role`). A statement with a parameter for
    # each posting's key went over the driver's 32,767 on a big board; this has one. With Company switched off
    # on Gates, no employer stops a posting (`gating.Rules`).
    roles: same_role.Known[uuid.UUID] = same_role.Known()
    model = None
    async with off_turn.reading(session, apart=slices) as reader:
        rules = await gating.rules(reader, owner, profile, today=owner_today(now))
        waiting = list((await reader.scalars(query)).all())
        if waiting:
            roles = await same_role.known_roles(reader, owner)
    if slices:
        await db.release_if_free(session)  # what was read so far is plain data: training holds no writer turn
    if waiting:
        async with off_turn.reading(session, apart=slices) as reader:
            model = await classifier.train(reader, owner)
    if slices:
        await db.release_if_free(session)  # the reading is done: the verdicts are written a slice at a time
    stopped: dict[str, int] = {}
    routes = {ROUTE_ROLES: 0}
    candidates = []
    looked_at, asked = 0, 0
    left = reads  # the pages still to be read for a place (a negative number is no limit)
    coop = Cooperate()
    for start in range(0, len(waiting), CHUNK):
        chunk = await _read(session, waiting[start:start + CHUNK], fresh=slices)
        if slices:
            await db.release_if_free(session)  # the slice is read: judging it holds no writer turn
        looked_at += len(chunk)
        if fetch is not None and left != 0 and rules.gates.places_gated:
            # The first `left` postings of the slice that want their places read, in the order they were first seen.
            wanting = [p for p in chunk if roles.find(p.company, p.title, same_role.posting_links(p),
                                                      key=p.match_key) is None and postings.wants_places(p)]
            reading = wanting if left < 0 else wanting[:left]
            if reading:
                left = left if left < 0 else left - len(reading)
                asked += len(reading)
                await postings.read_places(session, fetch, reading, now)
                if slices:
                    await db.release_if_free(session)  # what the pages gave is saved: the judging holds no row
        verdicts = []
        for posting in chunk:
            await coop()
            verdicts.append(_judge(posting, roles, rules, model))
        # The postings of the slice still open, unfiltered and no role's, read again in id order under this writer's
        # turn; one filtered since it was read is left out.
        open_still = (await as_read.lock(session, JobPosting, chunk, where=waiting_where(owner),
                                         refresh=False)).ids if slices else None
        for posting, verdict in zip(chunk, verdicts, strict=True):
            if open_still is not None and posting.id not in open_still:
                continue  # filtered by another run since it was read, or held by one: not judged twice
            word = _write(posting, verdict, rules, now)
            if word is not None:
                stopped[word] = stopped.get(word, 0) + 1
            else:
                routes[ROUTE_ROLES] += 1
                candidates.append({"posting": posting, "why": verdict.why, "route": ROUTE_ROLES,
                                   "prob": verdict.prob})
        await session.flush()
        if slices:
            await checkpoint.reached()  # a slice is whole: the driver of a long read saves here
            await db.release_if_free(session)
    return {"looked_at": looked_at, "candidates": candidates, "stopped": stopped, "routes": routes,
            "model": model.summary() if model else None, "read": asked}


async def _read(session: AsyncSession, ids: list[uuid.UUID], *, fresh: bool) -> list[JobPosting]:
    """The postings of one slice, in the order of `ids` (one that is gone since the ids were read is left out), read
    with nothing locked. Read again as they are now when the filter writes in slices (`fresh`: the session may hold an
    older reading of one)."""
    query = select(JobPosting).where(JobPosting.id.in_(ids))
    if fresh:
        query = query.execution_options(populate_existing=True)
    rows = {p.id: p for p in (await session.scalars(query)).all()}
    return [rows[i] for i in ids if i in rows]


@dataclass(frozen=True)
class _Verdict:
    """What the filter says of one posting, made with no row touched: the role it is (`role`), the gate that stops it
    (`stop`), or, when it passes, why it goes on (`why`) and the classifier's probability (`prob`)."""

    role: uuid.UUID | None = None
    stop: gating.Stop | None = None
    why: str | None = None
    prob: float | None = None


def _judge(posting: JobPosting, roles: same_role.Known[uuid.UUID], rules: gating.Rules,
           model: classifier.Model | None) -> _Verdict:
    """The verdict on one posting: a pure reading of its columns (none of the columns a verdict writes is read)."""
    role = roles.find(posting.company, posting.title, same_role.posting_links(posting), key=posting.match_key)
    if role is not None:
        return _Verdict(role=role)
    employer = rules.index.of(posting.employer_id, posting.company)
    stop = gating.check(gating.posting_subject(posting, employer), rules)
    if stop is not None:
        return _Verdict(stop=stop)
    why, prob = rank(model, posting)
    return _Verdict(why=why, prob=prob)


def _write(posting: JobPosting, verdict: _Verdict, rules: gating.Rules, now: datetime) -> str | None:
    """Put the verdict on the posting, as the one filter that judged and wrote together did. Returns the words it is
    counted under when it is stopped or a role already (`already a role`), None for a posting that goes on."""
    posting.route = posting.route_why = posting.fit_prob = posting.judged_by = posting.judged_by_version = None
    posting.nearest_role = None
    for column, value in gating.POSTING_CLEARED.items():
        setattr(posting, column, value)
    if verdict.role is not None:
        posting.opportunity_id, posting.filtered_at = verdict.role, now
        return "already a role"
    posting.filtered_at = now
    if verdict.stop is not None:
        for column, value in gating.posting_stop_values(verdict.stop, rules).items():
            setattr(posting, column, value)
        return verdict.stop.posting_words
    posting.fit_prob = verdict.prob
    posting.filtered_by, posting.route, posting.route_why = None, ROUTE_ROLES, verdict.why
    return None
