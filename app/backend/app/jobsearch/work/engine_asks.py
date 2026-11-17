"""What the engine does when the page asks it something (the split of the app into a page and an engine).

A route whose answer needs work the page must not do (a profile re-rank, a site probe, adding a link, an owner action)
calls `engine_calls.ask(session, owner, name, args, wait_s)`. The ask is two rows saved together: the fact `ask:<id>`
(`{"name", "args", "asked_at"}`, JSON) and a to-do of the topic `ask` with the key <id>. The dispatcher's handler
(`handlers.engine_ask`) hands each ask to the function registered here under its name, saves the function's return
value as the fact `answer:<id>` (JSON), and then tells the page twice: one datagram (`notice.tell_page`, which does
nothing and returns False in a program that has no ports, as in the tests) and the waiters of this process
(`notice.waiters.notify`, which is how a one-program run such as a test hears its own answer; in the two-program
instance the engine's process has no waiter, so it costs nothing). The page re-reads the answer fact once when its
wait ends, so a notice that never came costs nothing.

A function registered here is `async def fn(session, owner, args)` and returns plain data that is not None (a dict).
`session` is a writer's session with no transaction open yet. A function that fetches or waits calls
`await db.release(session)` before it does (the repository's rule: the writer's turn is for writing), and may write
rows through `session`; the handler saves the answer in the same session and commits. A function that raises leaves the
to-do to back off and try again, and the page's wait ends in None.

At every start the dispatcher posts the topic `ask` once with the key `*` (and the hour's sweep does too): the handler
reads that as "every ask younger than a day that has no answer", which is how an ask whose to-do was lost, or whose
engine was down, is answered. Facts older than a day are forgotten by the hour tick (`facts.age_out`)."""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import db, notice
from app.jobsearch import facts
from app.jobsearch.work import engine_calls
from app.jobsearch.work.queue import topics

log = logging.getLogger(__name__)

#: The kind a notice, a waiter and the to-do are filed under (`engine_calls.ASK_KIND` is the same word).
KIND = "ask"

Ask = Callable[[AsyncSession, str, dict[str, Any]], Awaitable[Any]]

#: The functions, by the name the page asks for.
_asks: dict[str, Ask] = {}


def register(name: str, fn: Ask) -> None:
    """Say which function answers the ask called `name`. Asking again with the same name replaces the first."""
    _asks[name] = fn


def is_registered(name: str) -> bool:
    """Whether a function answers the ask called `name` in this process."""
    return name in _asks


async def run_here(session: AsyncSession, owner: str, name: str, args: dict[str, Any]) -> Any:
    """The whole app in one process (the test seam, `resumebot serve`) has no other program to ask: the function of the
    ask runs here and now, in the caller's own session, and what it returns comes back as `engine_calls.ask` would give
    it (plain data, read back from JSON). What the caller wrote so far is saved first, as `ask` saves it before it
    waits; no row is written for the ask. Raises `KeyError` when nothing is registered under `name`
    (`is_registered`)."""
    await db.release(session)
    result = await _asks[name](session, owner, args)
    await session.commit()
    return json.loads(json.dumps(result))


async def unanswered(owner: str, keys: Iterable[str], now: datetime) -> list[tuple[str, dict[str, Any]]]:
    """The asks to answer, as (id, the ask's JSON read): those named by `keys` (an ask's id), or every ask when `keys`
    holds `*`; only those with no `answer:<id>` yet and asked less than a day before `now`. Read on a reader, so a look
    that finds nothing takes no writer's turn. A fact that cannot be read is logged and left."""
    named = tuple(keys)
    async with db.read_session_factory()() as reader:
        if topics.ANY in named:
            asked = await facts.get_many(reader, owner, "ask:")
        else:
            asked = {}
            for key in named:
                text = await facts.get(reader, owner, f"ask:{key}")
                if text is not None:
                    asked[f"ask:{key}"] = text
        answered = await facts.get_many(reader, owner, "answer:")
    cutoff = now - facts.FORGOTTEN_AFTER
    due: list[tuple[str, dict[str, Any]]] = []
    for fact_key, text in asked.items():
        ask_id = fact_key.removeprefix("ask:")
        if f"answer:{ask_id}" in answered:
            continue
        try:
            ask = json.loads(text)
            if not isinstance(ask["name"], str):
                raise TypeError("the name of the ask is not text")
            fresh = datetime.fromisoformat(ask["asked_at"]) >= cutoff
        except (ValueError, KeyError, TypeError):
            log.warning("engine ask %s cannot be read; left alone", ask_id)
            continue
        if fresh:
            due.append((ask_id, ask))
    return due


#: The asks being answered in this process now. The dispatcher runs each ask apart (`service.PER_KEY`) and a sweep's `*`
#: run names them all, so a second run that finds an ask already being answered leaves it to the first.
_answering: set[str] = set()

#: How many asks one run answers at once: the ask lane's own width (`queue.loop.LANES`), so a slow ask (a re-gate) does
#: not hold the quick ones read with it.
SLOTS = 2


async def answer(owner: str, ask_id: str, ask: dict[str, Any]) -> None:
    """Run the function of the ask, save its return value as `answer:<id>` in the same transaction, commit, then tell
    the page. Raises `LookupError` when nothing is registered under the ask's name. An ask this process is answering
    already is left to that run."""
    name = ask["name"]
    fn = _asks.get(name)
    if fn is None:
        raise LookupError(f"no engine ask is registered under {name!r}")
    if ask_id in _answering:
        return
    _answering.add(ask_id)
    try:
        async with db.session_factory()() as session:
            result = await fn(session, owner, ask.get("args") or {})
            await facts.put(session, owner, f"answer:{ask_id}", json.dumps(result))
            await session.commit()
    finally:
        _answering.discard(ask_id)
    notice.tell_page(owner, [(KIND, ask_id)])
    notice.waiters.notify(KIND, [ask_id])


async def answer_due(owner: str, keys: Iterable[str], now: datetime) -> int:
    """Answer every ask `unanswered` finds, up to `SLOTS` at once, so a slow one does not hold the rest. An ask whose
    function raises is logged and the others are still answered; the first error is raised at the end, so the
    dispatcher keeps the to-do and backs off. Returns how many it tried."""
    due = [(i, a) for i, a in await unanswered(owner, keys, now) if i not in _answering]
    slots = asyncio.Semaphore(SLOTS)
    failures: list[Exception] = []

    async def one(ask_id: str, ask: dict[str, Any]) -> None:
        async with slots:
            try:
                await answer(owner, ask_id, ask)
            except Exception as exc:
                log.warning("engine ask %s (%s) failed", ask_id, ask.get("name"), exc_info=True)
                failures.append(exc)

    await asyncio.gather(*(one(i, a) for i, a in due))
    if failures:
        raise failures[0]
    return len(due)


async def _profile_after_save(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, str]:
    """What the page's save or restore of a profile part leaves to the engine. `args` is
    `{"kind", "version", "how"}`: the part, the version just stored (committed before the ask) and how it came to be, in
    the words a role's history says it ("was saved"). A new Hard pass checks every posting and role against the gates
    again (`after_save.run`, which takes the engine's `regate:<owner>` lock itself); a new Prioritization works every
    score out again, a page to a turn (`profile_store.works_out_again`); no other part runs anything. The answer is
    `{"said": <the sentence>}` (empty when there is nothing to say), and the same sentence is kept as the fact
    `said:<kind>:<version>`. The line of the scores comes first, as a save always said it."""
    from app.jobsearch.profile import after_save  # engine modules: imported where used, never at the page's import
    from app.jobsearch.profile import store as profile_store

    kind, version, how = str(args["kind"]), int(args["version"]), str(args["how"])
    said: list[str] = []
    await after_save.run(session, owner, kind, version, how, said)
    line = await profile_store.works_out_again(session, owner, kind, version)
    if line:
        said.insert(0, line)
    sentence = " ".join(said)
    await facts.put(session, owner, f"said:{kind}:{version}", sentence)
    return {"said": sentence}


register("profile.after_save", _profile_after_save)


# --- the asks of the site and employer routes -------------------------------------------
# The page saves the change and asks; the engine does the fetch or the re-gate. Each imports its engine module inside
# the function, so loading this file loads none of them.


async def _site_probe(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`site.probe`, args `{site_id}`: test an `auto` site that has not been tested, from here (`read_probe.settle`: the
    polite fetch, at most 15 seconds) and write what was found on its row. Answers `{read, note}`; both are None when
    the site is gone, is not `auto`, or was tested already."""
    from app.sites import read_probe
    from app.sites.models import Site

    site = await session.get(Site, uuid.UUID(args["site_id"]))
    if site is None or site.owner != owner:
        return {"read": None, "note": None}
    found = await read_probe.settle(session, site)
    return {"read": found.read if found else None, "note": found.note if found else None}


async def _site_regate(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`site.regate`, args `{cause}`: check every posting in the log and every role again against the gates now in force
    (`regate.run`), after a site to avoid was added or taken off the list. `cause` is the words the history says.
    Answers `{said}`, the line the re-gate says of what it changed."""
    from app.jobsearch.gating import regate

    report = await regate.run(session, owner, cause=str(args["cause"]))
    return {"said": report.said}


async def _employer_regate(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`employer.regate`, args `{employer_id, by}`: an employer's gate was changed and saved by the page
    (`employers.edit` with `defer_gate_change`). Check the log and the roles again, write the trail line, then settle
    which employers are watched, which the roles it moved can change (the order the save had when it did both itself).
    Answers `{said}`, or `{said: None}` when the employer is gone."""
    from app.jobsearch.employers import core as employers
    from app.jobsearch.models import Employer
    from app.sites import where

    employer = await session.get(Employer, uuid.UUID(args["employer_id"]))
    if employer is None or employer.owner != owner:
        return {"said": None}
    report = await employers.gate_changed(session, owner, employer, str(args["by"]))
    await db.release_if_free(session)  # the check and its line are committed; settling the watch is a turn of its own
    await where.settle_watch(session, owner)
    return {"said": report.said}


register("site.probe", _site_probe)
register("site.regate", _site_regate)
register("employer.regate", _employer_regate)


async def _add_link(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`POST /roles`: the page asks for `role.add_link` with `{"url": <the text the owner pasted>}`. Puts the
    job through the gates with database work only (no page is read, and the page does not wait for this) and answers
    what `add_link.add` answers (`role` or `posting`, `already`, `said`, `stage`, ...). A text the app cannot use
    answers `{"refused": <the words>}`, which the page makes a 422 (the page has checked the text itself first, so this
    is only for a text that reads
    differently here)."""
    from app.jobsearch.browser import add_link, link_text  # the engine's own reader: the page never loads it

    try:
        return await add_link.add(session, owner, args["url"], args.get("posting"))
    except link_text.LinkError as refused:
        return {"refused": str(refused)}


register("role.add_link", _add_link)


# --- the asks of the role and employer routes -------------------------------------------------
# The page saves the owner's change and asks; the engine does the reading, the research or the settling that followed
# the save. Each imports its engine modules inside the function, so loading this file loads none of them. An ask that
# wakes someone commits first: a wake goes out only after the commit.


async def wake_for(role_id: uuid.UUID | str, done: dict[str, Any]) -> None:
    """Wake what the asked work left, as the routes did after their commit: the analyst when research was newly asked
    (`done["research"]`), the writer when a packet was asked (`done["packet"]`), and one pass of the dispatcher when a
    sourcing, a rank or new research is waiting."""
    research = done.get("research")
    fresh = bool(research and not research["already"])
    if fresh:
        await engine_calls.knock(str(role_id), reason=engine_calls.ANALYSIS_ASKED)
    if done.get("packet"):
        await engine_calls.knock(done["packet"], reason=engine_calls.PACKET_ASKED)
    if done.get("source") or done.get("rank") or fresh:
        await engine_calls.dispatch_now()


async def _role_research(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`role.research`, args `{role_id}`: the owner asked for the research of a role (`analysis.ask`: the role is put
    in the analyst's line through the process, which reads the network to begin). Commits, wakes the analyst when the
    ask was new, and answers `{asked: <what analysis.ask said>}`, or `{refused: {status, message}}` for a refusal (no
    such role 404), after which nothing is changed."""
    from app.errors import DomainError
    from app.jobsearch.research import analysis

    try:
        asked = await analysis.ask(session, owner, uuid.UUID(args["role_id"]))
    except DomainError as exc:
        await session.rollback()
        return {"refused": {"status": exc.status, "message": str(exc)}}
    await session.commit()
    if not asked["already"]:
        await engine_calls.knock(asked["role"], reason=engine_calls.ANALYSIS_ASKED)
    return {"asked": asked}


async def _role_settle(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`role.settle`, args `{role_id, cause}`: the page saved a change the role's score and gates read (a corrected
    class, `analysis.correct_class` with `defer_settle`). Settle the role again (`settle.roles`: the expectation, the
    score, the gates). Answers `{said}`, the line the gates wrote in its history, or None when they wrote none or the
    role is gone."""
    from app.jobsearch.models import Opportunity
    from app.jobsearch.research import settle

    role = await session.get(Opportunity, uuid.UUID(args["role_id"]))
    if role is None or role.owner != owner:
        return {"said": None}
    settled = await settle.roles(session, [role], cause=str(args["cause"]))
    return {"said": settled.gated.get(role.id)}


async def _role_read_first(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`role.read_first`, args `{role_id}`: the page saved a move to Ready to Launch that was held for the read
    (`board.move` with `defer_reads`: `Opportunity.ready_asked_at` is the mark). Put the role at the front of what it
    still lacks (`favorites.ask_reads`), commit, and wake what was asked. Answers `{done}` (the keys `ask_reads` gives),
    or `{done: None}` when the role is gone or the owner moved it elsewhere since (that withdraws the mark)."""
    from app.jobsearch.models import Opportunity
    from app.jobsearch.pipeline import favorites

    role = await session.get(Opportunity, uuid.UUID(args["role_id"]), populate_existing=True)
    if role is None or role.owner != owner or role.ready_asked_at is None:
        return {"done": None}
    done = await favorites.ask_reads(session, owner, role)
    await session.commit()
    await wake_for(role.id, done)
    return {"done": done}


async def _role_fast_track(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`role.fast_track`, args `{role_id}`: the page starred a role (`favorites.set_favorite` with
    `defer_fast_track`). Fast-track it (`favorites.fast_track`: its reads first, or its place in Ready to Launch and its
    packet), commit, and wake what was asked. Answers `{done}` (`moved`, `source`, `rank`, `research`, `packet`), or
    `{done: None}` when the role is gone or no longer starred."""
    from app.jobsearch.models import Opportunity
    from app.jobsearch.pipeline import favorites

    role = await session.get(Opportunity, uuid.UUID(args["role_id"]), populate_existing=True)
    if role is None or role.owner != owner or not favorites.is_favorite(role):
        return {"done": None}
    done = await favorites.fast_track(session, owner, role)
    await session.commit()
    await wake_for(role.id, done)
    return {"done": done}


async def _employer_refresh(session: AsyncSession, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    """`employer.refresh`, args `{employer_ids, cause, keep_gated_out}`: the page merged two employer records or split
    roles out of one (`records.merge` and `split` with `defer_refresh`). Work the gates and scores of the roles at these
    records out again (`employer_research.after_a_change`, which runs in a savepoint and logs a failure rather than
    undo the merge or split). Answers `{refreshed: True}`."""
    from app.jobsearch.research import employer as employer_research

    await employer_research.after_a_change(
        session, owner, [uuid.UUID(i) for i in args["employer_ids"]], cause=str(args["cause"]),
        keep_gated_out=bool(args["keep_gated_out"]))
    return {"refreshed": True}


register("role.research", _role_research)
register("role.settle", _role_settle)
register("role.read_first", _role_read_first)
register("role.fast_track", _role_fast_track)
register("employer.refresh", _employer_refresh)
