"""What the job-search tools do, apart from the MCP plumbing (`doors/scout.py`): `jobs.profile` (the scout's task),
`jobs.titles_next` and `jobs.titles_report` (a title session's: the roles of the one-off tagging and the header lines
of a role's posting), `jobs.report` (the scout's), `jobs.boards` and `jobs.board_found` (the scout's), `jobs.parse_next`
and `jobs.parse_report` (the posting parser's, `jobs/parse.py`), and `jobs.match_next` and `jobs.match_report` (the role
check's, `jobs/check.py`; the door calls those two modules directly). Each is `async (session, owner, ...) -> dict` and
returns plain data (a batch of items is held to `app.planengine.budget.AGENT_PAYLOAD_BUDGET_CHARS`; the owner's own
text goes whole); a refusal is a `RunError` the plumbing turns into `{"ok": false, "reason": ...}`.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import roster
from app.inbox import events
from app.jobsearch import ats, process, stages
from app.jobsearch.address import is_board_link
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import employer_list, resolve
from app.jobsearch.fetch.polite import public_url
from app.jobsearch.gating import gate_facts
from app.jobsearch.missions import record
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import Employer, Mission, MissionRun, Opportunity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.views import for_scout, for_titles
from app.jobsearch.states import FetchKind
from app.jobsearch.work import hand_outs, instruction_files, tagging, titles
from app.planengine import handouts
from app.planengine.budget import payload_chars
from app.sites import where
from app.timeutil import utc_now

#: The most answers one jobs.titles_report call takes (a batch is `titles.PER_SESSION`).
MAX_ANSWERS = 1000


async def profile_reply(session: AsyncSession, owner: str) -> dict:
    """The scout's task and profile: the owner's text, whole."""
    profile = await profile_store.current(session, owner)
    if not profile.is_set():
        raise run.RunError("the owner hasn't said what to look for yet (no company or target role in their profile)")
    scout = await roster.ensure_roster(session, owner)
    paused = scout.paused
    open_run = await run.open_run(session, owner)
    task = None
    if open_run is not None and not paused:
        where = await run.where_is(session, open_run)
        steps = {w["step"]: w for w in (where or {}).get("waiting_for", [])}
        waiting = steps.get(process.SCOUT_STEP)
        if waiting is not None:
            payload = waiting["payload"]
            task = {
                "run_id": str(open_run.id), "step": payload.get("name"), "focus": open_run.focus,
                "reported_so_far": len(open_run.reported),
                "report_with": f"jobs.report (batches of up to {run.MAX_BATCH} roles; the last call says done=true)",
                "response_template": await instruction_files.read(
                    session, owner, "templates", payload.get("template") or ""),
            }
    known = await session.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.owner == owner))
    reply = {
        "ok": True,
        "task": task,
        "note": None if task else (
            "The owner has paused you. Stop here: there is nothing to do until they resume you." if paused
            else "No search is open. Stop here: there is nothing to do until one starts."
        ),
        "roles_already_known": known,
        "profile": for_scout(profile),  # the owner's own text, whole
    }
    if task is not None and open_run is not None:
        title = "Find employers' job boards" + (f": {open_run.focus}" if open_run.focus else "")
        await handouts.sent(
            session, owner, open_run.id, reply, title=title,
            process=process.ROUND, step=process.SCOUT_STEP, connection=scout,
        )
        await events.scout_handed_task(session, owner, open_run.id)
    return reply


def _fit_to_budget(reply: dict, rows: list, item_of, budget: int, key: str = "candidates") -> dict:
    """Add `rows` to `reply[key]` as `item_of` shapes them, as many as the budget holds; the rest are counted in
    `more`."""
    for index, p in enumerate(rows):
        item = item_of(p)
        if reply[key] and payload_chars({**reply, key: [*reply[key], item]}) > budget:
            reply["more"] = len(rows) - index
            break
        reply[key].append(item)
    return reply


TAG_HOW = (
    "An item of kind `role` is a role the owner already has: say which one target role it matches. Judge its title "
    "and company against the target roles in `what_to_look_for`, one at a time, and answer with jobs.titles_report: "
    "{id, role: the id of the one it matches, or none when it matches none}."
)
TITLES_AGAIN = "Then stop: the next batch is for the next session."
#: What a session that has had its batch is told when it asks again (`hand_outs.session_took`).
BATCH_DONE = "You have had your batch. Stop: the next batch is for the next session."


async def titles_next_reply(session: AsyncSession, owner: str, session_id: str | None = None, *,
                            claim: str | None = None) -> dict:
    """The next items for a title session: while the one-off tagging is open (`tagging.py`), a batch of the roles found
    before target roles, as items of kind `role` (id, title, company), to be matched to a target role (that works with
    no search open), with what the owner looks for (the company and each target role); and the roles whose posting's
    header lines wait for a short read of the facts the owner's gates stop on, as items of kind `facts`
    (`gate_facts.py`). Handing the roles out is noted on the job, with the version of What to look for they are matched
    under, so a role no one answers is not handed out again and again.

    One batch a session (`titles.PER_SESSION`): a session named by `session_id` that has had its batch is told to stop,
    and the next session takes the next batch. A session started for a hand-out (`claim`, `hand_outs.py`) is handed
    the items claimed for it (what the ask chose: `work.claim_items`), never what another session's claim holds."""
    claimed = set(await hand_outs.take(session, owner, hand_outs.TITLES, claim, session_id) or []) if claim else set()
    job = await tagging.settle(session, owner)
    facts_waiting = await gate_facts.waiting(session, owner, skip_leased=True, light=True)
    holds_facts = await gate_facts.session_holds(session, owner, session_id)
    if job is None and not facts_waiting and not holds_facts:
        raise run.RunError("no search is open, so there are no titles to judge")
    scout = await roster.ensure_roster(session, owner)
    if scout.paused:
        return {"ok": True, "postings": [], "more": 0, "how": "The owner has paused you. Stop here."}
    if job is not None:  # one session at a time takes a batch: the next sees its leases
        await hand_outs.lock_holder(session, job)
    if (job is not None and hand_outs.session_took(job, "handed", session_id)[0]) or holds_facts:
        return {"ok": True, "postings": [], "more": 0, "batch_done": True, "how": BATCH_DONE}
    to_tag = await tagging.waiting(session, job, skip_leased=True) if job is not None else []
    # With a claim, exactly its items (when any is still there); the others wait for their own sessions.
    exact = bool(claimed & {str(x.id) for x in [*to_tag, *facts_waiting]})

    def own(rows: list, limit: int) -> list:
        return [x for x in rows if str(x.id) in claimed][:limit] if exact else rows[:limit]

    roles = own(to_tag, titles.PER_SESSION)
    # Gates first: the roles whose header lines wait for a short read (`gate_facts.py`), items of kind `facts`.
    facts_roles = own(facts_waiting, gate_facts.BATCH)
    facts_items = [await gate_facts.item(session, r) for r in facts_roles]
    reply: dict = {"ok": True}
    if not (facts_items and not roles):  # a session handed only header lines needs no target roles
        reply["what_to_look_for"] = for_titles(await profile_store.current(session, owner))
    how = [TAG_HOW] if roles else []
    if facts_items:
        how.append(gate_facts.HOW)
    reply.update({"postings": [], "more": 0, "how": " ".join([*how, TITLES_AGAIN])})
    rows = [*roles, *facts_items]
    # A reply over 50,000 characters goes unseen, because Claude Code saves anything over that to a
    # file; the door now declares the larger size (`budget.RESULT_SIZE_META`), so a batch is held to
    # `titles.REPLY_CHARS`. The rest are counted in `more`.
    _fit_to_budget(reply, rows, lambda x: x if isinstance(x, dict) else tagging.item(x), key="postings",
                   budget=titles.REPLY_CHARS)
    shown = rows[: len(reply["postings"])]
    role_rows = [x for x in shown if isinstance(x, Opportunity)]
    shown_facts = [r for r, i in zip(facts_roles, facts_items, strict=True) if any(i is x for x in shown)]
    if job is not None:
        tagging.hand_out(job, role_rows, await profile_store.newest_version(session, owner, "criteria"))
        hand_outs.note_session(job, "handed", session_id, len(role_rows), 0)
    gate_facts.hand_out(shown_facts, session_id)
    await session.flush()
    reply["more"] = (len(to_tag) - len(role_rows)) + (len(facts_waiting) - len(shown_facts))
    return reply


async def titles_report_reply(session: AsyncSession, owner: str, answers: list[dict]) -> dict:
    """Take a title session's answers: a role of the one-off tagging gets the target role it matches, or none; an item
    of kind `facts` is the short read of a posting's header lines. The app closes the tagging itself when no role is
    left."""
    if not isinstance(answers, list) or not answers:
        raise run.RunError("send answers as a list of {id, role}, one for each role you were handed to match "
                           "(for a header-line item: {id, kind: facts, ...})")
    if len(answers) > MAX_ANSWERS:
        raise run.RunError(f"send at most {MAX_ANSWERS} answers at a time")
    # Gates first: answers of kind `facts` are the short reads of a posting's header lines (`gate_facts.py`), taken
    # apart from the tagging's.
    facts_answers = [a for a in answers if isinstance(a, dict) and str(a.get("kind") or "").lower() == gate_facts.KIND]
    answers = [a for a in answers if not any(a is f for f in facts_answers)]
    facts = await gate_facts.take_answers(session, owner, facts_answers) if facts_answers else None
    job = await tagging.open_job(session, owner)
    if facts is not None and not answers:
        return {"ok": True, "taken": facts["taken"], "tagged": 0, "gated": facts["stopped"],
                "problems": facts["problems"][:10], "titling_done": True, "more_to_tag": 0,
                "next": ("Fix the answers named in problems and send them again with jobs.titles_report; then stop."
                         if facts["problems"] and not facts["taken"] else "Stop: the next batch is for the next "
                                                                         "session.")}
    if job is None:
        raise run.RunError("no search is open, so there is nothing to answer")
    result = await titles.answer(session, job, answers)
    if not result["taken"]:
        raise run.RunError("nothing was taken: " + "; ".join(result["problems"][:10]))
    still_tagging = await tagging.settle(session, owner)
    more_roles = len(await tagging.waiting(session, still_tagging)) if still_tagging is not None else 0
    finished = still_tagging is None
    return {
        "ok": True, "taken": result["taken"] + (facts["taken"] if facts else 0),
        "tagged": result["tagged"], "gated": facts["stopped"] if facts else 0,
        "problems": [*(facts["problems"] if facts else []), *result["problems"]][:10], "titling_done": finished,
        "more_to_tag": more_roles,
        "next": ("That was the last of the titles. Stop: you are finished." if finished
                 else "Fix the answers named in problems and send them again with jobs.titles_report; then stop: the "
                      "next batch is for the next session." if result["problems"]
                 else "Stop: the next batch is for the next session."),
    }


#: How many employers whose job board is unknown the scout is asked to find in one search.
BOARDS_ASKED = 10
#: What `jobs.boards` adds to its reply when an employer carries `list_websites` (the owner's employer list: docs/PRD.md
#: 28.3). The scout's instructions say the same, but the owner may have edited them, so the reply says it too.
LIST_NOTE = (" An employer's list_websites are from the owner's own employer list: a first clue to its own site, "
             "unchecked. Some are dead, parked or the wrong company, so open each one and confirm it is this "
             "employer's before you follow it to the job list; when it is not, ignore it and find the site as you "
             "would without it.")
#: The mark of the scout's answer for an employer (a board, or only its own careers page). An answer that
#: named no job system once left an employer on the list, to be offered again and again while the scout echoed back the
#: careers link it was given. So an answered employer is not offered again, and no timer brings it back
#: (the owner, 2026-11-05): it is offered again only when the app's reading of its site fails for good, which takes the
#: mark away (`employers.ask_scout_again`: its job system's board is gone, or no means can read its careers page).
ASKED = employers.BOARD_ASKED
#: What an employer's record says of the scout night that handed it to the scout: `{"night": the night (the scout
#: mission's run), "search": the search it was handed in}`. An employer handed over and not answered is not handed over
#: again in that night, and the next night hands it over again: a night that goes on until none is left would otherwise
#: go round and round on it (a night would loop through searches on one item nobody answered).
OFFERED = "board_offered"


def _answered(employer: Employer) -> bool:
    return bool(((employer.sources or {}).get(ASKED) or {}).get("at"))


def _offered_in(employer: Employer, night: uuid.UUID | None) -> bool:
    return night is not None and ((employer.sources or {}).get(OFFERED) or {}).get("night") == str(night)


async def scout_night(session: AsyncSession, owner: str) -> uuid.UUID | None:
    """The scout night going now (the scout mission's run), or None: a search the owner started by hand has no night."""
    return await session.scalar(
        select(MissionRun.id).join(Mission, Mission.id == MissionRun.mission_id)
        .where(Mission.owner == owner, Mission.key == record.SCOUT, MissionRun.outcome == "running", record.own_run())
        .order_by(MissionRun.started_at.desc()).limit(1))


async def _boards_queue(session: AsyncSession, owner: str, night: uuid.UUID | None) -> tuple[list[Employer], dict]:
    """The employers whose job board the app doesn't know yet and that the scout may be handed now, in the order it is
    handed them (docs/PRD.md 28.3): an employer with a role in play (`stages.OPEN`) first, then the most roles, then by
    name; and each one's number of roles. Not in it: a gated employer, one the scout found no board for, one it has
    answered for (`ASKED`: until the app's reading of its site fails for good), and one it was handed already in this
    `night`."""
    counts = dict((await session.execute(select(Opportunity.employer_id, func.count()).where(
        Opportunity.owner == owner, Opportunity.employer_id.is_not(None)).group_by(Opportunity.employer_id))).all())
    in_play = set((await session.scalars(select(Opportunity.employer_id).where(
        Opportunity.owner == owner, Opportunity.employer_id.is_not(None), Opportunity.status.in_(stages.OPEN))
        .distinct())).all())
    rows = (await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.ats.is_(None), Employer.gated.is_(False)))).all()
    waiting = [e for e in rows if e.fetch_kind != FetchKind.BOARD_NOT_FOUND and not _answered(e)
               and not _offered_in(e, night)]
    return sorted(waiting, key=lambda e: (e.id not in in_play, -counts.get(e.id, 0), e.name)), counts


async def boards_waiting(session: AsyncSession, owner: str, night: uuid.UUID | None) -> list[str]:
    """The ids of the employers left that the scout has not been handed in this scout night (`_boards_queue`); with no
    night, every employer that waits."""
    return [str(e.id) for e in (await _boards_queue(session, owner, night))[0]]


async def boards_handed_out(session: AsyncSession, owner: str, search_ids: list[str]) -> bool:
    """Whether `jobs.boards` handed the scout any employer in any of these searches."""
    return bool(search_ids) and await session.scalar(select(Employer.id).where(
        Employer.owner == owner, Employer.sources[OFFERED]["search"].as_string().in_(search_ids)).limit(1)) is not None


async def boards_answered_since(session: AsyncSession, owner: str, since: datetime) -> bool:
    """Whether the scout has answered for any employer's job board since `since` (`board_found`: a board, or none to
    find). Being handed an employer is the app's doing; an answer is the scout's work (docs/PRD.md 25.12)."""
    when = (since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)).isoformat()
    return await session.scalar(select(Employer.id).where(
        Employer.owner == owner, Employer.sources[ASKED]["at"].as_string() >= when).limit(1)) is not None


async def boards_reply(session: AsyncSession, owner: str) -> dict:
    """The employers whose job board the app doesn't know yet, those with a role in play first and then those with the
    most roles (docs/PRD.md 28.3): the scout finds each one's board, so the app can read it directly from then on. In a
    scout night each employer is handed over once, answered or not (`OFFERED`). An employer the owner's employer list
    names (`employer_list`) carries the list's websites as `list_websites`, a first clue that is the owner's word and
    unchecked, and the reply says so; nothing is made or changed from it."""
    night = await scout_night(session, owner)
    queue, counts = await _boards_queue(session, owner, night)
    rows = queue[:BOARDS_ASKED]
    if night is not None:
        search = await run.open_run(session, owner)
        offered = {"night": str(night), "search": str(search.id) if search else None}
        for e in rows:
            e.sources = {**(e.sources or {}), OFFERED: offered}
        await session.flush()
    sample = dict((await session.execute(select(Opportunity.employer_id, func.max(Opportunity.url)).where(
        Opportunity.employer_id.in_([e.id for e in rows] or [None])).group_by(Opportunity.employer_id))).all())
    clues = await employer_list.websites_for(session, owner, rows)
    return {"ok": True, "employers": [
        {k: v for k, v in {"company": e.name, "website": e.website, "careers_url": e.careers_url,
                            "list_websites": clues.get(e.id), "a_role_link": sample.get(e.id),
                            "roles": counts.get(e.id, 0)}.items() if v}
        for e in rows],
        "how": "For each, find the page that lists all its open jobs (its job system's board when it has one) and "
               "send it with jobs.board_found; send not_found with a few words when there is none to find."
               + (LIST_NOTE if clues else "")}


async def board_found_reply(session: AsyncSession, owner: str, company: str, url: str | None = None,
                            not_found: str | None = None) -> dict:
    """Save the job board the scout found for an employer: its system is read from the link at once. When the app had
    switched the employer's site off because it could not read its careers page, a board the app can read switches the
    site back on (`where.scout_found_board`), unless the board is another employer's."""
    employer = await employers.find(session, owner, company)
    if employer is None:
        raise run.RunError(f"no employer called {company!r}; send the company as jobs.boards gave it")
    # Answered, whatever it said: the employer is not offered again until the app's reading of its site fails.
    employer.sources = {**(employer.sources or {}), ASKED: {"at": utc_now().isoformat()}}
    if not_found:
        employer.fetch_note = f"board not found: {not_found.strip()[:200]}"
        employer.fetch_kind = FetchKind.BOARD_NOT_FOUND
        await session.flush()
        return {"ok": True, "saved": "not found", "company": employer.name}
    link = (url or "").strip()
    if not link.startswith(("http://", "https://")) or not public_url(link):
        raise run.RunError("send the board's whole address, starting with https://, and a public one (no sign-in, no "
                           "local or private host)")
    if not is_board_link(link):  # a job board's page is not the employer's careers site
        employers.learn_careers_url(employer, link, "found by the scout", by="scout")
    named = ats.parse(link)
    found = named.board if named else None
    if found and resolve.is_gone(employer, found):
        # The job system answered 404 for this board (`resolve.forget`), so it is not taken again, whoever names it: the
        # next read would find it gone and hand the employer over again, night after night. It counts as an answer that
        # named no job system: the mark is set above and the employer waits (docs/PRD.md 28.3).
        found = None
    if found and employer.ats_found_by != "owner":
        resolve.take(employer, found.system, found.parts, "analyst")  # keeps the boards known to be gone
        # An employer whose site the app switched off is read through this board from now on (docs/PRD.md 28.3), unless
        # another employer holds it and it carries not this employer's name: whose it is, `resolve.from_links` settles.
        held = await resolve.held_boards(session, owner)
        if not resolve.taken(held, employer, found.system, found.parts):
            await where.scout_found_board(session, employer)
    await session.flush()
    return {"ok": True, "company": employer.name, "system": ats.label(found.system) if found else "its own site"}


async def report_reply(session: AsyncSession, owner: str, roles: list[dict], done: bool) -> dict:
    open_run = await run.open_run(session, owner)
    if open_run is None:
        raise run.RunError("no search is open, so there is nothing to report to")
    try:
        result = await run.stage_report(session, open_run, roles, done)
    except run.RoundFailed:
        await session.commit()  # keep the record that this search failed, then tell the scout
        raise
    return {"ok": True, **result}
