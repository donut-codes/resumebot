"""One search across everything the app holds of the owner's job search (the owner, 2026-10-23): every role, whatever
its status (Closed with its reason and Gated out included), and the postings that are not roles yet (waiting in Added
links, stopped by the filter with its reason, or still to be read), `GET /api/v1/search`.

Each word typed must be in the title, the employer, the place, the status's name or the reason (a gate's, an ended
role's or the filter's), in any case, in any order: "senior acme remote" finds a senior role at Acme that is remote.
Roles come first, then postings; in each, the most recently found first. A result is the few fields a row needs
(`kind`, `id`, `title`, `company`, `where`, `status`, `label`, `lane`, `closed_reason`, `closed_label`, `reason`,
`favorite`, `fit_pct`, `found_at`, `url`); the role page or the board holds the rest. A page at a time, and only the
columns a row shows are read (never a posting's text or a role's research), so a search answers in a few milliseconds
over thousands of roles and postings. Each search also counts what matches in each status or place (`stages`)."""

import math

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.gating.stopped import TITLE_DID_NOT_MATCH
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import board, ended
from app.jobsearch.pipeline import from_posting as role_from_posting
from app.jobsearch.profile import store as profile_store
from app.timeutil import iso_or_none

PAGE = 25
MOST_PER_PAGE = 100
#: At most this many words of a search are looked for.
MOST_WORDS = 8
ROLE, POSTING = "role", "posting"
#: A posting's place, in words: waiting in Added links, stopped by the filter, or still to be read.
ADDED, STOPPED, WAITING = board.ADDED_LINKS, "stopped", "not_a_role_yet"
POSTING_LABELS = {ADDED: board.ADDED_LINKS_LABEL, STOPPED: "Stopped by the filter", WAITING: "Not a role yet"}


def words(q: str) -> list[str]:
    """The words of a search, lower case, at most `MOST_WORDS`. A NUL byte means nothing in a search (and Postgres
    refuses it)."""
    return " ".join((q or "").replace("\x00", "").lower().split()).split()[:MOST_WORDS]


def _statuses_named(w: str) -> list[str]:
    """The statuses whose name holds the word ("gated", "launch"); "closed" names every closed one."""
    named = [s for s, label in stages.LABELS.items() if w in label.lower()]
    return named + list(stages.CLOSED) if w in board.LABELS[board.CLOSED].lower() else named


def _end_reasons_named(w: str) -> list[str]:
    """The reasons an ended role ended whose words hold the word ("expired", "level")."""
    return [r for r, label in stages.END_REASON_LABELS.items() if w in label.lower()]


def _role_match(ws: list[str]):
    """Each word in the title, employer, place, a gate's reason, the status's name or an ended role's reason."""
    return [or_(Opportunity.title.icontains(w, autoescape=True), Opportunity.company.icontains(w, autoescape=True),
                Opportunity.location.icontains(w, autoescape=True),
                Opportunity.screened_out_reason.icontains(w, autoescape=True),
                Opportunity.status.in_(_statuses_named(w)),
                and_(Opportunity.status.in_(stages.ENDED), Opportunity.ended_reason.in_(_end_reasons_named(w))))
            for w in ws]


def _posting_match(ws: list[str]):
    return [or_(JobPosting.title.icontains(w, autoescape=True), JobPosting.company.icontains(w, autoescape=True),
                JobPosting.location.icontains(w, autoescape=True),
                JobPosting.filtered_by.icontains(w, autoescape=True))
            for w in ws]


def _not_a_role(owner: str):
    """The postings that are no role: open ones of the owner that no role was made of.

    A search reads them through the partial index `ix_js_postings_owner_search` (migration 0164), which holds every
    column it matches, counts, orders by and shows. Statistics say a NULL in `opportunity_id` matches few rows, so the
    planner chose that column's index and read every wide row; `likely()` takes that estimate away and the term still
    implies the index's own.

    `owner <> ''` (always true; a literal, which is what SQLite matches) is in the index's condition and only here:
    the queue's probes of owed postings name no such term, so they keep their own small partial indexes and cannot be
    served by this one."""
    return (JobPosting.owner == owner, func.likely(JobPosting.opportunity_id.is_(None)), JobPosting.is_open,
            JobPosting.owner != literal_column("''"))


#: Only what a row shows (`_role_row`, `_posting_row`).
_ROLE_COLUMNS = (Opportunity.id, Opportunity.title, Opportunity.company, Opportunity.location, Opportunity.move_type,
                 Opportunity.status, Opportunity.ended_reason, Opportunity.screened_out_reason, Opportunity.fit,
                 Opportunity.fit_breakdown, Opportunity.target_role, Opportunity.favorited_at, Opportunity.created_at,
                 Opportunity.url)
_POSTING_COLUMNS = (JobPosting.id, JobPosting.title, JobPosting.company, JobPosting.location, JobPosting.work_mode,
                    JobPosting.filtered_by, JobPosting.judged_by, JobPosting.first_seen, JobPosting.url,
                    JobPosting.apply_url)


def _role_row(r, span) -> dict:
    closed = r.status in stages.CLOSED
    key = board.closed_reason_key(r.status, r.ended_reason) if closed else None
    reason = (r.screened_out_reason if r.status == stages.GATED_OUT
              else ended.words(r) if r.status in stages.ENDED else None)
    return {
        "kind": ROLE, "id": str(r.id), "title": r.title, "company": r.company,
        "where": " · ".join(x for x in (r.location, r.move_type) if x) or None,
        "status": r.status, "label": stages.LABELS.get(r.status, r.status),
        "lane": board.CLOSED if closed else r.status,
        "closed_reason": key, "closed_label": board.CLOSED_LABELS.get(key) if key else None, "reason": reason,
        "favorite": r.favorited_at is not None, "fit_pct": scoring.percent_of(r, span),
        "found_at": iso_or_none(r.created_at), "url": r.url,
    }


def _posting_row(p, added: set) -> dict:
    place = ADDED if p.id in added else STOPPED if p.filtered_by else WAITING
    from app.jobsearch import judged  # who judged a posting, in the stopped list's words

    reason = (TITLE_DID_NOT_MATCH if p.judged_by == judged.TITLE_CHECK else p.filtered_by) if place == STOPPED else None
    return {
        "kind": POSTING, "id": str(p.id), "title": p.title, "company": p.company,
        "where": " · ".join(x for x in (p.location, p.work_mode) if x) or None,
        "status": place, "label": POSTING_LABELS[place], "lane": ADDED if place == ADDED else None,
        "closed_reason": None, "closed_label": None, "reason": reason, "favorite": False, "fit_pct": None,
        "found_at": iso_or_none(p.first_seen), "url": p.url or p.apply_url,
    }


async def find(session: AsyncSession, owner: str, q: str, page: int = 1, size: int = PAGE) -> dict:
    """One page of what matches every word of `q` (`words`): the roles first, then the postings that are no role.
    `roles` and `postings` say how many of each match, `total` both; `stages` how many match in each status (a role's)
    or place (a posting's: `added_links`, `stopped`, `not_a_role_yet`), only those with any, and `closed` how many of
    the roles are closed. An empty search finds nothing."""
    size = min(max(int(size), 1), MOST_PER_PAGE)
    ws = words(q)
    empty = {"q": " ".join(ws), "page": 1, "size": size, "pages": 1, "total": 0, "roles": 0, "postings": 0,
             "stages": {}, "closed": 0, "items": []}
    if not ws:
        return empty
    # `owner <> ''` is in the roles index's condition and only in a search, so no other statement on `owner` can pick it
    role_where = and_(Opportunity.owner == owner, Opportunity.owner != literal_column("''"), *_role_match(ws))
    posting_where = and_(*_not_a_role(owner), *_posting_match(ws))
    # grouped by an expression: grouped by the column itself the planner took the status index, which sorts the groups
    # and reads every wide row, and not `ix_js_opportunities_owner_search`, which holds all the columns it needs
    status = (Opportunity.status + "").label("status")
    by_stage = dict((await session.execute(select(status, func.count()).where(role_where).group_by(status))).all())
    n_roles = sum(by_stage.values())
    n_postings, stopped, added = (await session.execute(select(
        func.count(), func.count().filter(JobPosting.filtered_by.is_not(None)),
        func.count().filter(and_(*role_from_posting.waiting_added_where(owner)))).where(posting_where))).one()
    if n_postings:
        by_stage.update({k: n for k, n in ((ADDED, added), (STOPPED, stopped), (WAITING, n_postings - added - stopped))
                         if n})
    total = n_roles + n_postings
    pages = max(math.ceil(total / size), 1)
    page = min(max(int(page), 1), pages)
    start = (page - 1) * size
    items: list[dict] = []
    if start < n_roles:
        span = scoring.bounds(await profile_store.current(session, owner))
        rows = (await session.execute(select(*_ROLE_COLUMNS).where(role_where)
                                      .order_by(Opportunity.created_at.desc(), Opportunity.id)
                                      .offset(start).limit(size))).all()
        items += [_role_row(r, span) for r in rows]
    left = size - len(items)
    if left > 0 and n_postings:
        rows = (await session.execute(select(*_POSTING_COLUMNS).where(posting_where)
                                      .order_by(JobPosting.first_seen.desc(), JobPosting.id)
                                      .offset(max(start - n_roles, 0)).limit(left))).all()
        added = set(await session.scalars(select(JobPosting.id).where(
            *role_from_posting.waiting_added_where(owner), JobPosting.id.in_([p.id for p in rows])))) if rows else set()
        items += [_posting_row(p, added) for p in rows]
    return {**empty, "page": page, "pages": pages, "total": total, "roles": n_roles, "postings": n_postings,
            "stages": by_stage, "closed": sum(n for s, n in by_stage.items() if s in stages.CLOSED), "items": items}
