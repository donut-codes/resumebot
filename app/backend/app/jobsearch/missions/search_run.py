"""Starting, feeding and settling a search run. Everything here flushes; the route (or the MCP tool) commits.

A search run is one `SearchRun` row and one live instance of `jobsearch_round`. Starting it runs the process to its
agent step (the scout's), where it waits. The scout reports roles in batches (`stage_report`), which are only staged;
when it says it is done the process is told its step is complete and runs to its end in the same transaction: take
the roles, set aside the known ones, screen each, store, summarize. A posting that passes the gates is no part of the
round: it becomes a role at once (`pipeline/from_posting.py`). If the rest of the round fails, none of it is kept (it
ran
in a savepoint), the run is closed as failed with the reason, and the next search can start.
"""

import contextlib
import logging
import re
import uuid
from collections.abc import Collection
from datetime import UTC, datetime

from process_engine.models import COMPLETED, WAITING, ProcInstance
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agents import roster
from app.agents.roster import open_run
from app.agents.security import cap_length
from app.errors import DomainError
from app.inbox import events
from app.jobsearch import off_turn, process
from app.jobsearch.fit import scoring
from app.jobsearch.gating import sentence_gates
from app.jobsearch.measures.pay_place import WORK_MODES, as_date
from app.jobsearch.models import SearchRun
from app.jobsearch.postings import same_role, whole
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.scoring import ANSWERS, GIVEN_ANSWERS
from app.jobsearch.states import StartedBy
from app.jobsearch.work import hand_outs, stuck
from app.settings import settings
from app.web_address import http_url

log = logging.getLogger(__name__)

MAX_BATCH = 50
#: The most roles one search holds: what the scout reports is staged on the run until it ends.
MAX_ROLES_PER_RUN = 400
MAX_FIT = 15
#: The most characters of a work location a report keeps. A posting can name dozens of places and the work location gate
#: reads all of them, so a cut must not drop the one the owner works from.
MAX_LOCATION = 1000
#: The most characters of a pay line a report keeps: pay by place can be a line for each of several places, and the pay
#: gate reads the lowest and the highest figure in it.
MAX_PAY_TEXT = 400
#: A yearly pay figure a report may give, in dollars: under the first it is an hourly or monthly figure, over the second
#: a slip (cents, a phone number).
MIN_YEARLY_PAY, MAX_YEARLY_PAY = 1_000, 10_000_000


class RunError(DomainError):
    """The search can't start or take that report; plain words (a route turns it into a 4xx, a tool into a refusal)."""


class RoundFailed(RunError):
    """The rest of the round failed after the scout said it was done; the run is closed as failed (the caller
    commits that record, then tells the scout)."""


async def start_round(
    session: AsyncSession, owner: str, started_by: str = "manual", focus: str | None = None,
) -> SearchRun:
    """Start a search: one at a time, and only once the profile says what to look for. Runs the process to the scout's
    step."""
    if await open_run(session, owner) is not None:
        raise RunError("a search is already running")
    scout = await roster.connection_by_slug(session, owner, roster.SCOUT)
    if scout is not None and scout.paused:
        raise RunError("the scout is paused: resume it on Agent Settings to search")
    if not (await profile_store.current(session, owner)).is_set():
        raise RunError(
            "set what to look for first (the kind of employer or a target role, in your profile), so a posting can be "
            "judged")
    focus = (focus or "").strip() or None
    # The run, its process and the owner's notice are made in a savepoint: whatever stops the start leaves none of
    # them behind (an open run with no process would block every search until the sweep closed it).
    async with session.begin_nested():
        run = SearchRun(owner=owner, status="open", started_by=started_by, focus=focus)
        session.add(run)
        try:
            await session.flush()
        except IntegrityError:  # another start got in between the check above and this insert (one open run each)
            raise RunError("a search is already running") from None
        instance = await process.engine().start(
            session, process.ROUND,
            {"owner": owner, "run": str(run.id), "focus": run.focus or settings.default_focus},
            subject=f"jobsearch:{run.id}",
        )
        run.instance_id = instance.id
        if instance.status == "failed":
            raise RunError(f"the search couldn't start: {instance.error}")
        await session.flush()
        await events.search_started(session, owner, run)
    return run


def _line(value, field: str, whose: str, limit: int) -> str:
    """A report's text as one line, cut at `limit`: a list of texts is joined with "; " (a posting can name several
    places, and pay by place). Empty when the report gives none; anything else is refused."""
    if isinstance(value, list | tuple) and all(isinstance(x, str) for x in value):
        value = "; ".join(p for p in (" ".join(x.split()) for x in value) if p)
    elif value is not None and not isinstance(value, str):
        raise RunError(f"{whose}: {field} is text, or a list of texts")
    return " ".join((value or "").split())[:limit]


def _yearly_dollars(value, name: str, whose: str) -> int | None:
    """A yearly pay figure in whole dollars from a report: a number, or text such as "$121,869". None when the report
    gives none. A figure under `MIN_YEARLY_PAY` is no yearly pay (an hourly or monthly figure sent as a yearly one would
    pass or fail the pay gate on the wrong number), nor is one over `MAX_YEARLY_PAY`: refused, saying what to send."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, bool):
            raise ValueError(value)
        figure = int(round(float(str(value).replace("$", "").replace(",", "").strip())))
    except (ValueError, OverflowError):
        figure = None
    if figure is None or not MIN_YEARLY_PAY <= figure <= MAX_YEARLY_PAY:
        raise RunError(f"{whose}: {name} is a yearly figure in whole dollars (pay by the hour or the month goes in "
                       "comp, as the posting writes it: the app reads the figures)")
    return figure


def normalize_role(raw: dict) -> dict:
    """One role the scout reported, checked and tidied. Raises `RunError` naming what is wrong; free text is capped
    (never stored whole, never obeyed: it is data from the web)."""
    company = " ".join(str(raw.get("company") or "").split())
    title = " ".join(str(raw.get("title") or "").split())
    if not company or not title:
        raise RunError("every role needs a company and a title (a posting that names no employer is not reported)")
    url = http_url(raw.get("url"))
    if url is None:
        raise RunError(f"'{title}' at {company} needs the link to its posting (an http address)")
    # A report that answers the owner's asks (`fit_breakdown.asks`) sends no total: the app adds the points up. One
    # that doesn't (no asks set yet) still sends its own fit and the line behind it. The staged `fit` of an answering
    # report is therefore a placeholder 0, never the reader's verdict (what the reader said is in `fit_breakdown`):
    # `actions.scored` works the real fit out from the answers when the report is taken.
    answers = isinstance(raw.get("fit_breakdown"), dict) and isinstance(raw["fit_breakdown"].get("asks"), list)
    fit = 0
    if not answers:
        try:
            fit = int(raw.get("fit"))
        except (TypeError, ValueError):
            raise RunError(f"'{title}' at {company} needs fit_breakdown.asks: an answer to each of the owner's asks "
                           "(or, with no asks set, a fit score as a whole number)") from None
        if not -5 <= fit <= MAX_FIT:
            raise RunError(f"'{title}' at {company}: fit {fit} is out of range")
    evidence = " ".join(str(raw.get("fit_evidence") or "").split())
    if not evidence and not answers:
        raise RunError(f"'{title}' at {company} needs its fit evidence (one line: what carries the score, the gap)")
    mode = str(raw.get("work_mode") or "unknown").strip().lower()
    if mode not in WORK_MODES:
        raise RunError(f"'{title}' at {company}: work_mode is one of {', '.join(WORK_MODES)}")
    posted = raw.get("posted")
    if posted not in (None, "") and as_date(posted) is None:
        raise RunError(f"'{title}' at {company}: posted is a date like 2026-10-06 (leave it out if you can't read it)")
    out = {
        "company": cap_length(company, "company", 300), "title": cap_length(title, "title", 500), "url": url[:2000],
        "fit": fit, "fit_evidence": cap_length(evidence, "fit_evidence", 800), "work_mode": mode,
        "posted": as_date(posted).isoformat() if as_date(posted) else None,
    }
    whose = f"'{title}' at {company}"
    place = _line(raw.get("location"), "location", whose, MAX_LOCATION)
    if place:
        out["location"] = place
    pay = _line(raw.get("comp"), "comp", whose, MAX_PAY_TEXT)
    if re.search(r"\d", pay):  # a pay line with no figure in it ("not stated", "competitive") states no pay
        out["comp"] = pay
    for name, limit in (("board", 300), ("apply_url", 2000), ("ats", 120), ("sector", 200), ("level", 200),
                        ("comp_source", 200)):
        value = " ".join(str(raw.get(name) or "").split())
        if value:
            out[name] = value[:limit]
    if "apply_url" in out:  # it is shown as a link: only a web address is kept (a bare domain gets its https://)
        link = http_url(out["apply_url"])
        if link is None:
            del out["apply_url"]
        else:
            out["apply_url"] = link
    target =" ".join(str(raw.get("target_role") or "").split()).lower()
    if target and target not in ("none", "null"):
        out["target_role"] = target[:80]
    figures = {name: _yearly_dollars(raw.get(name), name, whose) for name in ("comp_min", "comp_max")}
    low, high = figures["comp_min"], figures["comp_max"]
    if low is not None and high is not None and low > high:
        raise RunError(f"{whose}: comp_min is over comp_max")
    out.update({name: figure for name, figure in figures.items() if figure is not None})
    breakdown = raw.get("fit_breakdown")
    if breakdown not in (None, "", {}):
        if not answers:
            raise RunError(f"'{title}' at {company}: fit_breakdown is {{asks: [{{id, answer, evidence}}]}}: answer "
                           "each of the owner's asks met, not or unknown")
        asked = []
        for item in breakdown["asks"]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            entry = {"id": str(item["id"])[:80], "answer": str(item.get("answer") or "").strip().lower(),
                     "evidence": " ".join(str(item.get("evidence") or "").split())[:400]}
            if entry["answer"] not in ANSWERS:
                raise RunError(f"'{title}' at {company}: the answer to ask {entry['id']!r} is one of "
                               f"{', '.join(GIVEN_ANSWERS)}")
            if item.get("count") not in (None, ""):
                try:
                    entry["count"] = max(1, int(item["count"]))
                except (TypeError, ValueError):
                    raise RunError(f"'{title}' at {company}: an ask's count is a whole number") from None
            asked.append(entry)
        out["fit_breakdown"] = {"asks": asked}
    gate_answers, gate_problems = sentence_gates.clean(raw.get("gate_answers"))
    if gate_problems:
        raise RunError(f"'{title}' at {company}: {gate_problems[0]}")
    if gate_answers:
        out["gate_answers"] = gate_answers
    description = str(raw.get("description") or "").strip()
    if description:
        if too_long := whole.too_long(description, f"{whose}: description"):
            raise RunError(too_long)
        out["description"] = description  # whole, as the reader sent it (docs/PRD.md 39.29)
    return out


async def stage_report(
    session: AsyncSession, run: SearchRun, raw_roles: list[dict], done: bool,
) -> dict:
    """Stage a batch of the scout's roles and, when it says it is done, run the rest of the round. Returns what the
    scout is told. A batch with any bad role is refused whole (nothing staged) with every problem named."""
    if run.status != "open":
        raise RunError("that search is already closed")
    if len(raw_roles) > MAX_BATCH:
        raise RunError(f"report at most {MAX_BATCH} roles at a time")
    roles, problems = [], []
    for index, raw in enumerate(raw_roles):
        try:
            role = normalize_role(raw if isinstance(raw, dict) else {})
        except (DomainError, ValueError) as exc:  # a role that does not fit (RunError, TooLong), or a value unreadable
            problems.append(f"role {index + 1}: {exc}")
            continue
        roles.append(role)
    profile = await profile_store.current(session, run.owner)
    role_ids = [t.id for t in profile.criteria.target_roles]
    if not problems:  # a role names the target role it matches, one of the owner's (docs/PRD.md 30.5)
        for index, role in enumerate(roles):
            if role.get("target_role") and role["target_role"] not in role_ids:
                problems.append(f"role {index + 1}: target_role {role['target_role']!r} is not one of the owner's "
                                f"target roles ({', '.join(role_ids) or 'none yet'})")
    if not problems and any(r.get("fit_breakdown") for r in roles):
        for index, role in enumerate(roles):
            problems += [f"role {index + 1}: {p}" for p in scoring.problems(role.get("fit_breakdown"), profile)]
    if not problems:  # a role's answers to the owner's sentence gates name only gates the owner has
        for index, role in enumerate(roles):
            problems += [f"role {index + 1}: {p}" for p in sentence_gates.problems(role.get("gate_answers"), profile)]
    if problems:
        raise RunError("nothing was saved; fix these and send the batch again: " + "; ".join(problems[:10]))
    if len(run.reported) + len(roles) > MAX_ROLES_PER_RUN:
        raise RunError(f"a search holds at most {MAX_ROLES_PER_RUN} roles; call jobs.report with done=true to finish")
    run.reported = [*run.reported, *roles]
    await session.flush()
    await events.scout_reported(session, run.owner, run, len(roles), done)
    if not done:
        return {"staged": len(roles), "total": len(run.reported), "done": False}
    # Save what was staged before the round is finished, where the transaction is this call's own to end: the turn the
    # staging took is let go, so `finish_round`'s warm-up reads on a reader. A failed finish kept the staging anyway.
    await db.release_if_free(session)
    return {"staged": len(roles), "total": len(run.reported), "done": True, **await finish_round(session, run)}


async def scout_at_work(session: AsyncSession, owner: str) -> bool:
    """Whether a runner is in a search session now (its last word is recent): a second is not started beside it."""
    now = datetime.now(UTC)
    rows = await roster.runner_rows(session, owner)
    return any(r.state == "working" and r.kind == roster.SCOUT and now - r.seen_at <= roster.LIVE_FOR for r in rows)


async def free(session: AsyncSession, owner: str, now: datetime | None = None, *, mine: Collection[uuid.UUID] = (),
               scope: str | None = None) -> list[uuid.UUID]:
    """The open search's id when a session could be started for its step (the scout's own), else none: a search that
    another ask holds a claim on and that no session has started is not handed to a second one (`hand_outs.py`), and
    none is started while a runner is in a search session (`scout_at_work`). With `scope` "owners", only a round the
    owner started (Run now, `started_by` manual): the pool's own rounds and the schedule's wait for the owner's off
    hours. What makes `search` the answer to "which work waits" (`session_kinds.next_kind`) and what a claim holds."""
    found = await open_run(session, owner)
    if found is None:
        return []
    if scope == "owners" and found.started_by != StartedBy.MANUAL.value:
        return []
    if str(found.id) in await hand_outs.held(session, owner, hand_outs.SEARCH, now) - {str(m) for m in mine}:
        return []
    return [] if await scout_at_work(session, owner) else [found.id]


async def waiting_steps(session: AsyncSession, run: SearchRun) -> list[str]:
    """The steps the run's process waits at now (the scout's, or the round's own research)."""
    where = await where_is(session, run)
    return [w["step"] for w in (where or {}).get("waiting_for", [])]


async def finish_round(session: AsyncSession, run: SearchRun) -> dict:
    """The scout is done: let the process run to its end in one savepoint. The end can be a wait: the round's own
    research (docs/PRD.md 25.13) holds the process at its analyst's step while the run itself is closed and done."""
    engine = process.engine()
    # Read what the failure path needs now: a rolled-back savepoint expires what it touched, and an async session
    # can't load it again on the way out.
    run_id, instance_id, count, owner = run.id, run.instance_id, len(run.reported), run.owner
    # The report steps ask for the owner's roles index, and making it is seconds of the loop: warm the shared cache
    # here in a reader's session (no writer turn) so the engine's call finds it kept. Only warms; nothing is used.
    try:
        async with off_turn.reading(session) as reader:
            await same_role.known_roles(reader, owner)
    except Exception:  # a warm-up that fails changes nothing: the steps ask for the index themselves
        log.warning("couldn't warm the roles index before search %s was finished", run_id, exc_info=True)
    # The warm-up may have read with the writer's turn free, and another writer (the finish time's `wind_down`,
    # `close_unfinished`) may have finished this search meanwhile: look at the run again, which takes the turn the
    # write needs anyway. A search that is no longer open is neither completed nor failed again; what is stored is told.
    await session.refresh(run)
    if run.status != "open":
        return {"summary": run.summary, "counts": run.counts}
    try:
        async with session.begin_nested():
            instance = await engine.complete(session, instance_id, process.SCOUT_STEP, {"count": count})
            if instance.status == WAITING and process.ROUND_ANALYST_STEP in await waiting_steps(session, run):
                pass  # with the analyst: `analysis.settle_rounds` ends the step once the finds are researched
            elif instance.status != COMPLETED:
                raise RunError(instance.error or f"the process {instance.status}")
    except Exception as exc:  # whatever stopped it, the run is closed as failed, nothing half-done
        log.exception("search %s couldn't be finished", run_id)
        await fail_run(session, run, f"The search couldn't be finished: {exc}", instance_id, owner=owner)
        raise RoundFailed(f"the search couldn't be finished: {exc}") from exc
    await session.refresh(run)
    return {"summary": run.summary, "counts": run.counts}


async def fail_run(
    session: AsyncSession, run: SearchRun, why: str, instance_id=None, owner: str | None = None,
) -> None:
    """Close a run as failed and stop its process, so the next search can start. Never raises."""
    with contextlib.suppress(Exception):  # a rolled-back savepoint expired the run; read it again before using it
        await session.refresh(run)
    instance_id = instance_id or run.instance_id
    # Closing the run matters more than closing the instance, but a cancel that fails is logged (`stuck.py`).
    await stuck.cancel_instance(session, instance_id, why, f"search {run.id}")
    run.status, run.ended_at, run.summary = "failed", datetime.now(UTC), why[:500]
    await session.flush()
    try:
        async with session.begin_nested():
            await events.search_failed(session, owner or run.owner, run, why)
    except Exception:  # the notice is a courtesy; the run is closed either way
        log.exception("couldn't tell the owner a search failed")


async def close_unfinished(session: AsyncSession, run: SearchRun, why: str) -> str:
    """A search the scout never said was done: what it reported is kept (the round runs on it, as if it had said
    done), and only a search with nothing reported is closed as failed. Returns "kept" or "failed"."""
    if not run.reported:
        await fail_run(session, run, f"{why}, so it was closed. Search again.")
        return "failed"
    try:
        await finish_round(session, run)
    except RoundFailed:
        return "failed"
    run.summary = f"{run.summary or ''} ({why}; what it reported was kept.)".strip()[:1000]
    await session.flush()
    return "kept"


async def wind_down(session: AsyncSession, run: SearchRun, stop: str, words: str) -> str:
    """End an open search now, keeping what it found: the finish time came, or Claude's usage limit was hit, before the
    scout said it was done (docs/PRD.md 25.12). What the scout reported is stored through the rest of the round
    (`finish_round`). The round's own research does not start (`counts.stopped` is what tells it).
    Stopping is not a search's failure: one with nothing reported ends done, not failed. `stop` is the key kept on the
    run (`finish` or `usage`) and `words` what it is called in its summary. Returns "kept" or "failed"."""
    if run.status != "open":
        return "kept"
    run.counts = {**(run.counts or {}), "stopped": stop}
    await session.flush()
    try:
        await finish_round(session, run)
    except RoundFailed:
        return "failed"
    said = f"{run.summary or ''} (Stopped: {words}. What it found was kept; the rest waits for the next search.)"
    run.summary = said.strip()[:1000]
    await session.flush()
    return "kept"


async def settle_stuck(session: AsyncSession, owner: str, now: datetime | None = None,
                       since: datetime | None = None) -> int:
    """A search whose scout never finished is closed (`close_unfinished`). It is stuck when nothing has been heard of
    it for `stuck_minutes`: the last time a session of the round said it was alive, else when it started (counted from
    `since` when that is later: the end of peak hours, when work was held). Returns how many."""

    async def find(cutoff: datetime) -> list[SearchRun]:
        last_sign = func.coalesce(SearchRun.session_seen_at, SearchRun.started_at)
        return list((await session.scalars(select(SearchRun).where(
            SearchRun.owner == owner, SearchRun.status == "open", last_sign < cutoff))).all())

    async def close(run: SearchRun) -> None:
        await close_unfinished(session, run, "The scout didn't finish this search")

    return await stuck.settle_stuck(session, now=now, since=since, what="search", find=find, close=close)


async def where_is(session: AsyncSession, run: SearchRun) -> dict | None:
    """What the process says the open run waits for (the scout's step, with its role, instruction and template)."""
    if run.instance_id is None:
        return None
    instance = await session.get(ProcInstance, run.instance_id)
    if instance is None:
        return None
    return await process.engine().where_is(session, run.instance_id)
