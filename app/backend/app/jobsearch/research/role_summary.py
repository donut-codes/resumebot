"""A researched role's summary (the owner, 2026-10-23): three short lines at the top of the role's pane, below the fit,
pay, ask and company: what the company does (one sentence), what the role is (one sentence), and why this role is
attractive to the owner (one or two sentences, grounded in what they look for and in what the research found,
written to them as "you" and without drawbacks, which the pane shows elsewhere).

A role has one only when it reaches Second Impression or Ready to Launch (the owner, 2026-10-24; a favorite goes to
Ready to Launch, so it is covered). The analyst's research does not write it: a summary alone is asked for, one analyst
turn that reads the research already kept, the posting's text and what the owner looks for, and sends the three lines
back, kept on the role's analysis under `SUMMARY_KEY`, with no second research. It is asked for:

- when a role enters one of those two stages without a current summary (`want`, called where the stage move is
  recorded, `models._record_stage_moves`, and where a research ends in one of them);
- for a role opened in the pane in one of those stages without one (`ask`, `POST /roles/{id}/summary`). Any other
  stage shows no summary and asks for none.

The analyst's lane hands it out after any research waiting (`analysis.free`, `analysis.analysis_reply`): the task
is `summary_task`, and the analyst sends it with `jobs.analysis_submit` (`take`). A summary handed out and never sent is
handed out again once its lease lapses, `MOST_TRIES` times at most. A new research replaces the analysis, summary and
marks with it. Nothing here guesses or writes for the owner: the "for you" line comes from their own profile at run
time. Services flush; the route or the tool commits."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import JSON, Integer, and_, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import jsonsql
from app.errors import DomainError
from app.jobsearch import stages
from app.jobsearch.models import Opportunity
from app.jobsearch.work.claim import claim_one

#: Where the summary is kept on the role's analysis, and the marks of a summary alone asked for: when it was asked
#: (`WANTED_KEY`), when a session took it and which (`CLAIMED_KEY`, `CLAIMED_BY_KEY`), and how often it was handed out
#: (`TRIES_KEY`).
SUMMARY_KEY, WANTED_KEY, CLAIMED_KEY, CLAIMED_BY_KEY, TRIES_KEY = (
    "summary", "summary_wanted", "summary_claimed", "summary_claimed_by", "summary_tries")
#: How long a summary handed out is the session's before another may take it: one short turn.
LEASE = timedelta(minutes=10)
#: A summary handed out this many times and never sent is not handed out again (the role shows none).
MOST_TRIES = 2
#: The voice summaries are written in, stamped on each (`v`). A summary of an older voice is asked again (`want`)
#: (2: written to the owner as "you", the "why" says only what is attractive; none had a stamp before).
SUMMARY_VERSION = 2
#: The stages a summary is asked for (`want`, `ask`).
BACKFILL_STAGES = (stages.READY_TO_LAUNCH, stages.SECOND_IMPRESSION)
#: The longest each line may be: a sentence, or two for the last.
LINE_CHARS, WHY_CHARS = 400, 800

#: What the analyst's task says when it is a summary alone.
SUMMARY_ONLY = ("This is a summary alone, not a research: the role was researched already. Do not research it, read "
                "no web page and ask for no other part. From `research` (what the research found), "
                "`posting_text_kept` and `what_to_look_for` (what the owner looks for in this role), write the "
                "summary as \"The summary\" in your instructions says (to the owner as \"you\", never \"the owner\"), "
                "and send only `role` and `summary` with "
                "mcp__jobsearch__jobs_analysis_submit.")
RESPONSE_SHAPE = {"role": "the role id above",
                  "summary": {"company": "What the company does, in one sentence.",
                              "role": "What the role is, in exactly one sentence.",
                              "why": "Why this role is attractive to you: one or two sentences, from what you look "
                                     "for and what the research found; specific, no hype, nothing invented, no "
                                     "drawbacks."}}


class SummaryError(DomainError):
    status = 422


def _now_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _field(key: str):
    """What the role's analysis says under `key`, as text (SQL), or null."""
    return jsonsql.field(Opportunity.analysis, key)


def _researched():
    from app.jobsearch.research import analysis  # it reads this module

    return Opportunity.analysis_state == analysis.DONE


def view(role: Opportunity) -> dict | None:
    """The role's summary for its pane, `{company, role, why}`, or None when it has none."""
    found = (role.analysis or {}).get(SUMMARY_KEY)
    if not isinstance(found, dict) or not any(found.get(k) for k in ("company", "role", "why")):
        return None
    return {k: found.get(k) or "" for k in ("company", "role", "why")}


def stale(role: Opportunity) -> bool:
    """Whether the role's summary was written in an older voice than `SUMMARY_VERSION` (one with no stamp is)."""
    found = (role.analysis or {}).get(SUMMARY_KEY)
    if not isinstance(found, dict):
        return False
    try:
        return int(found.get("v") or 0) < SUMMARY_VERSION
    except (TypeError, ValueError):
        return True


def _stale_sql():
    """The roles whose kept summary is of an older voice (SQL)."""
    version = jsonsql.field(Opportunity.analysis, SUMMARY_KEY, "v")
    return and_(_field(SUMMARY_KEY).is_not(None), func.coalesce(cast(version, Integer), 0) < SUMMARY_VERSION)


def askable(role: Opportunity) -> bool:
    """Whether a summary alone may be asked for this role now: researched, with none, not asked already and not given
    up on. What the pane reads to ask for one when the role is opened (`ask`)."""
    from app.jobsearch.research import analysis  # it reads this module

    a = role.analysis or {}
    return (role.status in BACKFILL_STAGES and role.analysis_state == analysis.DONE and view(role) is None
            and not a.get(WANTED_KEY) and int(a.get(TRIES_KEY) or 0) < MOST_TRIES)


def wanted():
    """The roles whose summary alone waits (SQL): researched, in Second Impression or Ready to Launch, asked for
    (`WANTED_KEY`) with none or with one of an older voice; and not handed out `MOST_TRIES` times."""
    return and_(_researched(), Opportunity.status.in_(BACKFILL_STAGES),
                _field(WANTED_KEY).is_not(None),
                or_(_field(SUMMARY_KEY).is_(None), _stale_sql()),
                func.coalesce(cast(_field(TRIES_KEY), Integer), 0) < MOST_TRIES)


def _free(moment: datetime):
    """The roles no session holds the summary of now: never taken, or taken longer ago than the lease."""
    return or_(_field(CLAIMED_KEY).is_(None), _field(CLAIMED_KEY) <= _now_text(moment - LEASE))


async def offerable(session: AsyncSession, owner: str, now: datetime | None = None) -> bool:
    """Whether a summary alone waits that no session holds."""
    moment = now or datetime.now(UTC)
    return await session.scalar(select(Opportunity.id).where(
        Opportunity.owner == owner, wanted(), _free(moment)).limit(1)) is not None


async def claim_next(session: AsyncSession, owner: str, now: datetime | None = None, *,
                     session_id: str | None = None) -> Opportunity | None:
    """The role whose summary alone has waited longest that no session holds, taken by the session that asks, or None.
    One statement finds it and marks it (`claim_one`), so two sessions asking at once get different roles."""
    moment = now or datetime.now(UTC)
    tries = func.coalesce(cast(_field(TRIES_KEY), Integer), 0) + 1
    marked = func.json_set(func.coalesce(Opportunity.analysis, literal_column("'{}'")),
                           literal_column(f"'$.{CLAIMED_KEY}'"), _now_text(moment),
                           literal_column(f"'$.{TRIES_KEY}'"), tries, type_=JSON)
    if session_id:
        marked = func.json_set(marked, literal_column(f"'$.{CLAIMED_BY_KEY}'"), session_id[:80], type_=JSON)
    else:
        marked = func.json_remove(marked, literal_column(f"'$.{CLAIMED_BY_KEY}'"), type_=JSON)
    return await claim_one(
        session, select(Opportunity).where(Opportunity.owner == owner, wanted(), _free(moment))
        .order_by(Opportunity.favorited_at.is_(None), _field(WANTED_KEY), Opportunity.analysed_at), analysis=marked)


def held(role: Opportunity) -> bool:
    """Whether a session holds this role's summary alone (it was handed out and not sent)."""
    a = role.analysis or {}
    return bool(a.get(CLAIMED_KEY)) and (view(role) is None or stale(role))


async def summary_task(session: AsyncSession, owner: str, role: Opportunity, context: str | None = None) -> dict:
    """The analyst's task for a summary alone: the role, what its research found, the posting's text, what the owner
    looks for in its target role and the response shape. The instructions are the analyst's own (their section "The
    summary"), sent when the session's system prompt does not hold them."""
    from app.jobsearch.employers import core as employers
    from app.jobsearch.models import Employer
    from app.jobsearch.postings import text as posting_text
    from app.jobsearch.profile import store as profile_store
    from app.jobsearch.profile.views import look_for_role
    from app.jobsearch.research import session_context
    from app.jobsearch.work import instruction_files

    profile = await profile_store.current(session, owner)
    employer = await session.get(Employer, role.employer_id) if role.employer_id else None
    a = role.analysis or {}
    research = {
        "notes": a.get("notes") or None, "for_owner": a.get("for_owner") or None,
        "pay": {k: (a.get("pay") or {}).get(k) for k in ("band", "market_value", "note")},
        "ask": (a.get("ask") or {}).get("ask") if isinstance(a.get("ask"), dict) else None,
        "required": [{"item": r.get("item"), "class": r.get("class")} for r in a.get("required") or []],
        "preferred": [{"item": r.get("item"), "class": r.get("class")} for r in a.get("preferred") or []],
        "responsibilities": [{"item": r.get("item"), "class": r.get("class")}
                             for r in a.get("responsibilities") or []],
    }
    task = {
        "summary_only": SUMMARY_ONLY,
        "role": {"id": str(role.id), "title": role.title, "company": role.company, "location": role.location,
                 "work_mode": role.move_type, "comp": role.comp, "fit_breakdown": role.fit_breakdown or None,
                 "posting_text_kept": (await posting_text.held(session, role)).text or None},
        "research": research,
        "employer": employers.view(employer) if employer else None,
        "what_to_look_for": look_for_role(profile.criteria, role.target_role) or None,
        "response_shape": RESPONSE_SHAPE,
    }
    if not await session_context.held(session, owner, session_context.ANALYSIS, context):
        task["instructions"] = await instruction_files.read(session, owner, "instructions", "jobsearch.analyst")
    return task


def _line(value: object, most: int) -> str:
    return " ".join(str(value or "").split())[:most]


def clean(found: object) -> dict | None:
    """The three lines as sent (`{company, role, why}`), each on one line and within its length, with when; None when
    none was sent or any line is empty."""
    if not isinstance(found, dict):
        return None
    out = {"company": _line(found.get("company"), LINE_CHARS), "role": _line(found.get("role"), LINE_CHARS),
           "why": _line(found.get("why"), WHY_CHARS)}
    if not all(out.values()):
        return None
    return {**out, "v": SUMMARY_VERSION, "at": datetime.now(UTC).isoformat()}


async def take(session: AsyncSession, owner: str, role: Opportunity, body: dict) -> dict:
    """Take a summary alone the analyst sends for a role it was handed one of (`held`): kept on the role's analysis,
    the marks gone. One with a line missing is refused, and the session may send it again."""
    summary = clean(body.get("summary"))
    if summary is None:
        return {"ok": False, "taken": False,
                "reason": "not taken: send summary with company, role and why, each a sentence (why may be two)"}
    a = {k: v for k, v in (role.analysis or {}).items() if k not in (WANTED_KEY, CLAIMED_KEY, CLAIMED_BY_KEY)}
    role.analysis = {**a, SUMMARY_KEY: summary}
    await session.flush()
    return {"ok": True, "taken": True, "summary": view(role)}


async def ask(session: AsyncSession, owner: str, role_id: uuid.UUID) -> dict:
    """The owner opened a role with no summary: a summary alone is asked for it. `asked` is False when there is nothing
    to ask (not researched, it has one, or it is asked already). The caller commits and wakes the runner."""
    role = await session.get(Opportunity, role_id)
    if role is None or role.owner != owner:
        raise SummaryError("no such role")
    if not askable(role):
        return {"asked": False}
    role.analysis = {**(role.analysis or {}), WANTED_KEY: _now_text(datetime.now(UTC))}
    await session.flush()
    return {"asked": True}


def want(role: Opportunity) -> bool:
    """Ask a summary alone for a researched role in Second Impression or Ready to Launch that has no current one, once:
    one asked already, one with a current summary, and one given up on are left. A stale summary stays shown and its
    tries start again. True when it was asked. The caller flushes."""
    from app.jobsearch.research import analysis  # it reads this module

    a = dict(role.analysis or {})
    if role.status not in BACKFILL_STAGES or role.analysis_state != analysis.DONE or a.get(WANTED_KEY):
        return False
    if view(role) is not None:
        if not stale(role):
            return False
        for key in (TRIES_KEY, CLAIMED_KEY, CLAIMED_BY_KEY):
            a.pop(key, None)
    elif int(a.get(TRIES_KEY) or 0) >= MOST_TRIES:
        return False
    role.analysis = {**a, WANTED_KEY: _now_text(datetime.now(UTC))}
    return True
