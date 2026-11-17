"""The actions the job-search processes name (`playbooks/jobsearch/*.bpmn`), in the process engine's default registry
(the one `process_engine.versions` checks an edited file against, so an owner's edit of a process loads as the shipped
one does; the site projects' actions have a registry of their own). Each is `async (session, instance, data) -> dict`;
the engine copies `data` in and stores the result under the step's output name.

The screen actions are pure (they only phrase a verdict); the thresholds that choose between them are the FEEL
conditions in `screen_one_role.bpmn`. The round's actions read and write the job-search tables through the session
the engine was given, and never commit (the route owns the transaction; the night's read, whose driver owns each step's
session, is saved at the safe points two of its loops name, `checkpoint.reached`).
"""

import logging
import uuid
from datetime import UTC, datetime

from process_engine.errors import ActionError
from process_engine.registry import actions as engine_actions
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.inbox import events
from app.jobsearch import off_turn, stages
from app.jobsearch.address import is_board_link
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.employers import not_checked, resolve, signals
from app.jobsearch.employers import pairs as employer_pairs
from app.jobsearch.fetch import polite
from app.jobsearch.fit import classifier, freshness, scoring
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import sentence_gates, sift
from app.jobsearch.imports.tracker import slugify
from app.jobsearch.measures.pay_place import Gates, as_date, match_key, role_facts
from app.jobsearch.models import ORIGIN_SCOUT, JobPosting, Opportunity, SearchRun, StageMove
from app.jobsearch.pipeline import quiet
from app.jobsearch.postings import index as postings
from app.jobsearch.postings import read_progress, role_postings, same_role, site_reads, whole
from app.jobsearch.postings import sourced as sourcing
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.views import engine_gates
from app.jobsearch.research import keep_current, prioritize
from app.jobsearch.states import StopGate, StopStep
from app.jobsearch.work import checkpoint, queue
from app.sites import job_systems, read_probe, where
from app.sites import registry as site_registry
from app.timeutil import owner_today

log = logging.getLogger(__name__)
registry = engine_actions
SOURCE = "backend/app/jobsearch/actions.py"


def _links(raw: dict) -> tuple:
    return raw.get("url"), raw.get("apply_url")


def best_by_key(reported: list[dict]) -> dict[str, dict]:
    """The scout's roles by match key; a role reported twice keeps its higher fit (the first on a tie). Twice is the
    same role in any writing (`same_role`): "Director, X" and "Director - X" are one. The fit is the staged report's:
    for one that answers the asks, the total `take_reported` added up, which every step after it finds there."""
    seen: same_role.Known[int] = same_role.Known()
    best: list[dict] = []
    for raw in reported or []:
        company, title = raw.get("company"), raw.get("title")
        at = seen.find(company, title, _links(raw))
        if at is None:
            at = len(best)
            best.append(raw)
        elif int(raw.get("fit") or 0) > int(best[at].get("fit") or 0):
            best[at] = raw
        seen.add(at, company, title, _links(raw))
    return {match_key(raw.get("company"), raw.get("title")): raw for raw in best}


async def _gates(session: AsyncSession, owner: str) -> Gates:
    """The owner's gates as the process compares them: the numbers, the places and the names on Hard pass's list. None
    set: every role gets through. The employers gated on their own records are not here: the one gate check stops a
    role at one by its record where the role is stored (`store`), by the rule the filter uses (the employer's name or
    an alias, by key; docs/PRD.md 39.11), not by a word of its name."""
    return engine_gates(await profile_store.current(session, owner))


async def _run(session: AsyncSession, data: dict) -> SearchRun:
    run = await session.get(SearchRun, uuid.UUID(data["run"]))
    if run is None:
        raise ActionError("no_run", "the search run this process belongs to isn't there")
    return run


# --- the round -------------------------------------------------------------------------------------------------


async def open_run(session, instance, data):
    return (await _gates(session, data["owner"])).for_process()


async def scored(session: AsyncSession, owner: str, raw: dict, profile, target_role: str | None = None,
                 seen: datetime | None = None) -> tuple[dict, dict | None]:
    """The report with a fit added up by code from the reader's answers to the owner's asks, the employer record's
    facts and the posting's age and likely ask (scoring.py); and the breakdown. The report as it came, and None, when
    it holds no answers (its own fit stands). Only the asks that apply to the role's target
    role are counted (docs/PRD.md 31.5): the one the report names, else `target_role` (the one a title
    match or the title pass gave the posting). The report comes back naming it, so the gates that follow judge it
    against its own tab's minimum. `seen`
    is when the app first saw the posting, when it has that time (`freshness.first_seen_by_key`): the hour a "posted
    within the last N days" ask counts from. This fit only chooses between two reports of one role (`best_by_key`): no
    role is stored with it, because the score is worked out last, after research (docs/PRD.md 39.20,
    `prioritize.work_out`)."""
    target_role = raw.get("target_role") or target_role
    if target_role and not raw.get("target_role"):
        raw = {**raw, "target_role": target_role}
    if not scoring.is_asks(raw.get("fit_breakdown")):
        return raw, None
    employer = await employers.find(session, owner, raw.get("company"))
    known = role_facts(raw, engine_gates(profile), owner_today())
    breakdown = scoring.compute(
        raw["fit_breakdown"], profile,
        {**employers.facts(employer), **gating.job_facts(gating.report_subject(raw, employer, target_role), profile,
                                                         owner_today())},
        facts={"age_days": known["age_days"], "ask": known["ask"], **freshness.facts(as_date(raw.get("posted")), seen)},
        target_role=target_role)
    return {**raw, "fit": breakdown["total"]}, breakdown


async def take_reported(session, instance, data):
    """Read each reported role for what the gates compare. A report that answers the asks is staged with a fit of 0
    (the app adds the points up, `run.normalize_role`), so every report's fit is worked out here and kept on the staged
    report: of two reports of one role, `best_by_key` then keeps the one that scores higher, here and in the steps
    after this one. That fit stays on the staged report: the role it becomes is stored with its answers and no score."""
    run = await _run(session, data)
    profile = await profile_store.current(session, data["owner"])
    gates = await _gates(session, data["owner"])
    on = owner_today()
    keys = [match_key(raw.get("company"), raw.get("title")) for raw in run.reported]
    # The target role the title pass gave a posting (docs/PRD.md 30.5), for a role whose report names none.
    matched = await _matched_roles(session, data["owner"], keys)
    # When the app first saw each posting, for the ask on a posting within the last so many days.
    seen = await freshness.first_seen_by_key(session, data["owner"], keys)
    run.reported = [(await scored(session, data["owner"], raw, profile, matched.get(key), seen.get(key)))[0]
                    for raw, key in zip(run.reported, keys, strict=True)]
    roles = [role_facts(raw, gates, on) for raw in best_by_key(run.reported).values()]
    return {"count": len(run.reported), "roles": roles}


async def dedupe(session, instance, data):
    """Set aside the roles the owner already has: the tracker's, an earlier search's, one a gate stopped. A role is one
    the owner has when it is the same role in any writing (`same_role`), not only to the letter, so a role the owner
    applied to does not come back as a new role because the employer's site writes the title with a dash and the tracker
    with a comma."""
    taken = data.get("taken") or {}
    roles = taken.get("roles") or []
    fresh, kin, reported = [], {}, {}
    if roles:
        known = await same_role.known_roles(session, data["owner"])
        reported = best_by_key((await _run(session, data)).reported)
        for role in roles:
            raw = reported.get(role["key"]) or {}
            owned = known.find(raw.get("company"), raw.get("title"), _links(raw), key=role["key"])
            if owned is None:
                fresh.append(role)
            else:
                kin[role["key"]] = owned
    # A posting the reader was handed for one of these is the owner's role now, as the filter would have made it had it
    # met the role first: left alone it would be handed to a reader again every night.
    # The posting's stored key is the one it was filtered under: an employer renamed since leaves it spelled the old
    # way, so the company and title it carries now are matched too.
    linked = 0
    for key, owned in kin.items():
        company, _, title = key.partition("|")
        linked += (await session.execute(update(JobPosting).where(
            JobPosting.owner == data["owner"], JobPosting.opportunity_id.is_(None),
            or_(JobPosting.match_key == key,
                and_(func.lower(func.trim(JobPosting.company)) == company,
                     func.lower(func.trim(JobPosting.title)) == title))
        ).values(opportunity_id=owned))).rowcount or 0
    # The postings now belong to roles the owner already has: those roles are looked at, and a posting that is some
    # role's may hold the text that role lacks (the hook posts this for a save through the session; a direct statement
    # posts beside itself).
    queue.post_roles(session, data["owner"], kin.values(), cause="core:actions.dedupe")
    queue.post(session, data["owner"], queue.ROLE_DETAILS, [""] if linked else [], cause="core:actions.dedupe")
    # and it may now have a current copy of one of them to rank (`cleansed._CURRENT` joins the copies to the role by the
    # link alone), which only the pool's pull reads.
    queue.post(session, data["owner"], queue.POOL_OWED, [""] if linked else [], cause="core:actions.dedupe")
    # `taken` already folded repeats within the report to one; what was reported beyond that is set aside too.
    return {"roles": fresh, "duplicates": int(taken.get("count") or 0) - len(fresh)}


async def _matched_roles(session: AsyncSession, owner: str, keys: list[str]) -> dict[str, str]:
    """The target role the title pass gave each posting not yet a role, by match key."""
    return dict((await session.execute(select(JobPosting.match_key, JobPosting.target_role).where(
        JobPosting.owner == owner, JobPosting.opportunity_id.is_(None), JobPosting.target_role.is_not(None),
        JobPosting.match_key.in_(keys or [""]))
    )).all())


def fit_words(role, span) -> str:
    pct = scoring.percent_of(role, span)
    return f"fit {pct}%" if pct is not None else f"fit {role.fit}"


async def store(session, instance, data):
    run = await _run(session, data)
    profile = await profile_store.current(session, data["owner"])
    rules = await gating.rules(session, data["owner"], profile)
    version = rules.criteria
    tab_ids = {t.id for t in profile.criteria.target_roles}
    raw_by_key = best_by_key(run.reported)
    verdicts = {v["key"]: v for v in (data.get("verdicts") or []) if v}
    kept = screened = 0
    on = owner_today()
    texts: dict[str, str] = {}
    for posting in (await session.scalars(select(JobPosting).where(
            JobPosting.owner == data["owner"], JobPosting.opportunity_id.is_(None), JobPosting.text.is_not(None),
            JobPosting.match_key.in_([r["key"] for r in (data.get("fresh") or {}).get("roles") or []] or [""])))).all():
        if text := whole.of_posting(posting):  # only a source's own whole text is a description (`whole.of_posting`)
            texts[posting.match_key] = text
    # The target role the title pass gave a posting (docs/PRD.md 30.5), for a role whose report names none.
    fresh_keys = [r["key"] for r in (data.get("fresh") or {}).get("roles") or []]
    matched = await _matched_roles(session, data["owner"], fresh_keys)
    boards = await employer_posting.job_board_hosts(session, data["owner"])
    for role in (data.get("fresh") or {}).get("roles") or []:
        raw, verdict = raw_by_key.get(role["key"]), verdicts.get(role["key"])
        if raw is None or verdict is None:
            continue
        keep = bool(verdict.get("keep"))
        employer = await employers.find_or_make(session, data["owner"], raw["company"])
        if texts.get(role["key"]):
            # The text the posting holds (the job system's own, or the page the app fetched) is
            # the role's, whole: a reader's own reading of it, when it sent one, is a summary of it (docs/PRD.md 39.29).
            raw = {**raw, "description": texts[role["key"]]}
        # The tab the reader or the title check named, if it is still one (a deleted tab's id is never kept on a role).
        target = raw.get("target_role") or matched.get(role["key"])
        target = target if target in tab_ids else None
        # A reader's answers to the owner's asks are kept as they were given, under each ask's words, and the role has
        # no score: it is worked out last, after research (docs/PRD.md 39.20). A report with no answers to give (no asks
        # set yet) keeps the fit it sent.
        answered = scoring.is_asks(raw.get("fit_breakdown"))
        answers = scoring.record([], scoring.answers_in(raw.get("fit_breakdown")), profile,
                                 prioritize.BY_READER) if answered else []
        # The one gate check on what is about to be stored (docs/PRD.md 39.11). The round's process has decided the
        # place, the pay, the age and an employer on Hard pass's list; the check also reads the employer's own gate,
        # the employer rules of All's tab and of the role's own (31.9), and a sentence the reader failed from the
        # posting (an unknown, a pass and no answer stop nothing). A rule's or an employer rule's own sentence is the
        # reason, as it always was.
        stop = gating.check(gating.report_subject(raw, employer, target), rules)
        if keep and stop is not None:
            own_line = stop.gate_id != gating.EMPLOYER and stop.gate in (StopGate.EMPLOYER, StopGate.RULE)
            keep, verdict = False, {**verdict, "reason": stop.says if own_line else stop.role_words, "gate": stop.gate}
        # Its stop as data (39.12): the process's own drop has the check's line when they name the same gate.
        same = stop is not None and not keep and stop.gate == verdict.get("gate")
        step, line_id = (stop.step, stop.gate_id) if same and stop is not None else (StopStep.GATE, None)
        notes = [verdict.get("note") or ""]
        if not (raw.get("posted") or "").strip():
            notes.append("posting date unverified")
        said = (raw.get("fit_evidence") or "").strip() or scoring.answers_line({"answers": answers}, profile, target)
        evidence = " | ".join(p for p in [said, (verdict.get("note") or "").strip()] if p)
        values = {
            "id": uuid.uuid4(), "owner": data["owner"], "slug": slugify(raw["company"], raw["title"]),
            "company": raw["company"], "title": raw["title"],
            # A kept role has its posting read (its raw copy is held): Landing Zone, then the stages move it on
            # (`pipeline.advance`).
            "status": stages.LANDING_ZONE if keep else stages.GATED_OUT,
            "fit": None if answered else verdict.get("fit"), "level": raw.get("level"),
            "comp": raw.get("comp") or _comp_text(raw),
            "comp_source": raw.get("comp_source") or ("posting" if (raw.get("comp") or _comp_text(raw)) else None),
            "location": raw.get("location"), "move_type": raw.get("work_mode"), "sector": raw.get("sector"),
            "found": on, "board": raw.get("board"), "url": raw.get("url"), "apply_url": raw.get("apply_url"),
            # The job board link the role was found at is kept for good, even once the employer's own posting is
            # found and becomes `url` (employers/follow.py).
            "board_url": raw.get("url") if is_board_link(raw.get("url"), boards) else None,
            "ats": raw.get("ats"), "notes": "; ".join(n for n in notes if n) or None,
            "match_key": role["key"], "fit_evidence": evidence or None,
            "screened_out_reason": None if keep else verdict.get("reason"),
            "screened_out_by": None if keep else (verdict.get("gate") or StopGate.OTHER.value),
            "screened_out_step": None if keep else step.value, "screened_out_gate_id": None if keep else line_id,
            "screened_out_hard_pass": None if keep else rules.hard_pass,
            "screened_out_criteria": None if keep else rules.criteria, "source_run": run.id,
            "origin": ORIGIN_SCOUT, "posted": as_date(raw.get("posted")),
            "employer_id": employer.id if employer else None, "fit_breakdown": {"answers": answers} if answered else {},
            # A reader's answers to the owner's sentence rules, each kept under the rule's words as they are now
            # (docs/PRD.md 39.13), so a rule reworded later is a new question. A rule that applies to the role and that
            # the reader left out is recorded as can't tell, so the role is not read again for it: only a rule that is
            # new or in new words, later, sends it back (39.15).
            "gate_answers": sentence_gates.record([], raw.get("gate_answers") or [], profile),
            # Only the posting's own text (above) is kept as the description: a reader's reading is a summary.
            "description": texts.get(role["key"]) or None,
            "description_at": datetime.now(UTC) if texts.get(role["key"]) else None,
            "target_role": target, "matched_by_version": version if target else None,
        }
        inserted = await session.execute(
            insert(Opportunity).values(values)
            .on_conflict_do_nothing(index_elements=["owner", "match_key"]).returning(Opportunity.id)
        )
        new_id = inserted.first()
        if new_id is not None:
            # A Core insert bypasses the session hook that writes a new role's stage move, so it is written here: the
            # Pipeline's "in" count reads these (models._record_stage_moves).
            session.add(StageMove(owner=data["owner"], role_id=new_id[0], from_status=None, to_status=values["status"]))
            kept, screened = (kept + 1, screened) if keep else (kept, screened + 1)
            made = await session.execute(update(JobPosting).where(
                JobPosting.owner == data["owner"], JobPosting.match_key == role["key"],
                JobPosting.opportunity_id.is_(None)).values(opportunity_id=new_id[0]).returning(JobPosting.id))
            if made.first() is None:
                # No posting read from a board became this role: the scout reported it directly. It enters the log as a
                # posting, unless the log already holds it in another writing (docs/PRD.md 39.4).
                await role_postings.log_reported(session, values)
        # A role made here (or none: the statements changed no row) is looked at, as the hook would for an ORM insert.
        queue.post_new_roles(session, data["owner"], [values] if new_id is not None else [], cause="core:actions.store")
    if kept:  # a kept role is in play: its employer is watched now when the owner asked for that (docs/PRD.md 29.9)
        await where.settle_watch(session, data["owner"])
    return {"kept": kept, "screened_out": screened}


async def follow_postings(session, instance, data):
    """Roles found on a job board, followed to their employer's own posting where the posting index holds it. A nicety
    to the round and to the night's read, never a reason to lose them: if it fails, what it did is rolled back, the
    failure is logged, and the process goes on.

    Where the session can release (`db.can_release`: the night's read, outside any process engine call) each phase is
    its own run of short turns: the follow and the sourcing commit their slices as they go, a phase that raises rolls
    back only its uncommitted part (the slices already committed stay), and the turn is let go between the phases. The
    step stays re-runnable: the follow, the gate facts and the sourcing each do what is not yet done. Inside an engine's
    call (`search_round`, `db.holding`) nothing can be committed or rolled back by half, so each phase keeps its
    savepoint, as before."""
    free = db.can_release(session)
    try:
        if free:
            followed = await employer_posting.follow_roles(session, data["owner"])
        else:
            async with session.begin_nested():
                followed = await employer_posting.follow_roles(session, data["owner"])
    except Exception:  # the roles and the read are kept whatever the matcher does
        log.exception("following job-board roles to their employers' postings failed")
        if free:
            await session.rollback()
        followed = {"looked": 0, "followed": 0}
    # The gate batches of the sourcing have sessions of their own: this one's turn is let go before they run.
    await db.release_if_free(session)
    # The nightly retry of sourcing, in code (the owner, 2026-10-15): a role in play that is not Sourced is tried again
    # with what the read and the follow just brought (`sourcing.sweep`).
    try:
        if free:
            await sourcing.sweep(session, data["owner"])
        else:
            async with session.begin_nested():
                await sourcing.sweep(session, data["owner"])
    except Exception:
        log.exception("sourcing the roles in code failed")
        if free:
            await session.rollback()
    return followed


async def summarize(session, instance, data):
    run = await _run(session, data)
    reported = int((data.get("reported") or {}).get("count") or 0)
    duplicates = int((data.get("fresh") or {}).get("duplicates") or 0)
    stored = data.get("stored") or {}
    kept, screened = int(stored.get("kept") or 0), int(stored.get("screened_out") or 0)
    found = (await session.scalars(
        select(Opportunity).where(Opportunity.source_run == run.id, Opportunity.status.in_(stages.OPEN))
        .order_by(Opportunity.created_at))).all()
    # A kept role has no score until it is researched (docs/PRD.md 39.20): the likeliest are the classifier's, and a
    # role that does have a fit (one a reader scored itself, with no asks to answer) says it.
    probs = await classifier.role_probabilities(session, data["owner"], [r.id for r in found])
    top = sorted(found, key=lambda r: (r.id not in probs, -probs.get(r.id, 0.0)))[:3]
    text = (
        f"Reviewed {reported} roles: {kept} kept, {screened} gated out, {duplicates} already known."
        if reported else "The scout reported no roles."
    )
    span = scoring.bounds(await profile_store.current(session, data["owner"]))
    best = [f"{r.title} at {r.company}" + (f" ({fit_words(r, span)})" if r.fit is not None else "") for r in top]
    if top:
        text += " Likeliest: " + "; ".join(best) + "."
    run.status, run.ended_at, run.summary = "done", datetime.now(UTC), text
    # Why a search was cut short (the finish time, Claude's usage limit: `run.wind_down`) stays on the run: it keeps its
    # own research from starting (analysis.plan_round).
    stopped = (run.counts or {}).get("stopped")
    run.counts = {"reported": reported, "kept": kept, "screened_out": screened, "duplicates": duplicates,
                  **({"stopped": stopped} if stopped else {})}
    await events.search_finished(
        session, data["owner"], run, reported, kept, screened, duplicates,
        best,
    )
    return {"text": text}


def _comp_text(raw: dict) -> str | None:
    low, high = raw.get("comp_min"), raw.get("comp_max")
    if low and high:
        return f"${int(low):,}-${int(high):,}"
    if low or high:
        return f"${int(low or high):,}"
    return None


# --- screening one role ----------------------------------------------------------------------------------------


def _verdict(data: dict, keep: bool, reason: str = "", note: str = "", fit=None, gate: StopGate | None = None) -> dict:
    role = data.get("role") or {}
    return {"key": role.get("key", ""), "keep": keep, "reason": reason, "note": note,
            "fit": role.get("fit") if fit is None else fit, "gate": gate}


def _gate_says(data: dict, gate: str) -> str:
    """The sentence of a gate that is on (the reason a role it stops is given); empty when the process wasn't given
    the gates' sentences. Where the role's own Hard pass tab is the gate that decided (docs/PRD.md 31.9), its sentence
    is the role's (`role.says`)."""
    return ((data.get("role") or {}).get("says") or {}).get(gate) or ((data.get("gates") or {}).get("says") or {}).get(
        gate, "")


def _why(sentence: str, detail: str) -> str:
    """A gate's sentence with what this role was judged on: "Pay must be at least $100,000 (the ask is $70,000)."."""
    return f"{sentence.rstrip('.')} ({detail})."


async def drop_excluded(session, instance, data):
    return _verdict(data, False, "excluded employer", gate=StopGate.EMPLOYER)


async def drop_location(session, instance, data):
    place = (data.get("role") or {}).get("location")
    sentence = _gate_says(data, "location")
    return _verdict(data, False, _why(sentence, f"posted: {place or 'no place'}") if sentence else
                    "location: not a way of working or a place you allow", gate=StopGate.LOCATION)


async def drop_comp(session, instance, data):
    role, gates = data["role"], data["gates"]
    judged, source = role["comp_judged"], role.get("comp_from") or "pay"
    sentence = _gate_says(data, "compensation")
    floor = role.get("comp_floor", gates["comp_floor"])
    return _verdict(data, False, _why(sentence, f"the {source} is ${int(judged):,}") if sentence else
                    f"comp floor: {source} ${int(judged):,} is under ${int(floor):,}", gate=StopGate.COMPENSATION)


async def drop_stale(session, instance, data):
    age, limit = int(data["role"]["age_days"]), int(data["role"].get("stale_days", data["gates"]["stale_days"]))
    sentence = _gate_says(data, "age")
    return _verdict(data, False, _why(sentence, f"this one is {age} days old") if sentence else
                    f"stale: posted {age} days ago, over {limit}", gate=StopGate.AGE)


async def keep(session, instance, data):
    role = data.get("role") or {}
    notes = []
    if role.get("location_ok") is None:
        notes.append("place not stated: check the work location before applying")
    return _verdict(data, True, "", "; ".join(notes), fit=role.get("fit"))


# The analysis process's steps (playbooks/jobsearch/analyse_role.bpmn) are in analysis.py and the packet process's
# (build_packet.bpmn) in packets.py; each module registers its own, in the same registry. Both import the process, so
# `process.load_actions` is what imports them.


# --- reading the employers' boards (playbooks/jobsearch/read_boards.bpmn, docs/PRD.md path 28) ---------------------


#: Careers pages read a night to find employers' job systems.
CAREERS_PAGES_A_NIGHT = 40
#: Descriptions read between two looks at the clock (the night's time limit, `polite.timeup`) and between two saves:
#: four times the reads begun at once (`postings.READ_AT_ONCE`), so the slice's slowest host does not hold the rest of
#: the width idle for long (docs/PRD.md 28.17).
DETAILS_AT_A_TIME = 100


async def resolve_links(session, instance, data):
    """Each employer's job system, found from the links the app holds (no network), then, for the employers whose site
    the owner switched on only, from their careers pages. Every employer with an address has its site (and one on a
    known system its reader) before and after, so an employer that gains a board is seen at once. A careers page the
    server is refused (robots.txt, a 403) sends that employer's site to the owner's browser, with the reason on its row
    (`read_probe.hand_refused`), instead of leaving it to wait for a project that would meet the same refusal; so does
    an employer on a system the server has no reader for and knows why (`read_probe.hand_no_reader`)."""
    owner = data["owner"]
    # Each phase is its own writer turn: what one wrote is whole before the next reads (later reads of the employers
    # refresh what the session holds, `populate_existing`).
    await where.seed_employer_sites(session, owner)
    await db.release_if_free(session)
    found = (await resolve.from_links(session, owner))["found"]
    await db.release_if_free(session)
    await where.seed_employer_sites(session, owner)
    await db.release_if_free(session)
    watched = {e.id for e in await where.watched_employers(session, owner)}
    await db.release_if_free(session)
    fetch = polite.make()
    try:
        pages = await resolve.from_pages(session, owner, fetch, limit=CAREERS_PAGES_A_NIGHT, only=watched)
        whole = await resolve.complete_boards(session, owner, fetch, only=watched)
    finally:
        await fetch.close()
    await read_probe.hand_refused(session, owner, pages["refused"])
    await where.seed_employer_sites(session, owner)
    await db.release_if_free(session)
    # Last, so an employer whose system was found just now is seen: one on a system the server has no reader for is the
    # owner's browser's, not a site project's to try.
    await read_probe.hand_no_reader(session, owner)
    await db.release_if_free(session)
    await read_probe.recheck_unreadable(session, owner)  # a site switched off as unreadable is looked at again monthly
    return {"found": found + sum(pages["found"].values()) + whole["completed"]}


async def _recipes_upkeep(session, owner) -> None:
    """The live recipes are read with the rest (`site_reads.refresh`); this asks for a new recording of a stale one
    (`browser/recipes/live.py`). A pass that cannot finish is logged and rolled back:
    it never
    stops the night's read."""
    from app.jobsearch.browser.recipes import live as recipe_live

    try:
        async with session.begin_nested():
            await recipe_live.maintain(session, owner)
    except Exception:
        log.exception("asking for new recordings of the stale recipes failed; the night's read goes on")


async def read_boards(session, instance, data):
    """Reads the sites the owner switched on (and no others), but not one whose skill broke and whose project is still
    learning its new shape, nor an employer on a job system the owner switched off (Where to look, `job_systems_off`).
    An employer on a known job system is read through its built-in adapter (`postings/index.py`); a site whose skill is
    a
    recipe the scout learned is read through that skill (`site_reads.py`, never both for one site), and both go into
    the same posting index. A read that says a skill no longer fits (docs/PRD.md 29.4) retires it, so the next night's
    Scout mission starts that site's project; nothing starts now."""
    owner = data["owner"]
    fetch = polite.make()
    read = site_reads.combine({}, {})
    try:
        skipped = (await where.unreadable_employer_ids(session, owner)
                   | await job_systems.switched_off_ids(session, owner))
        built_in, by_skill = await site_reads.plan(session, owner, [
            e for e in await where.watched_employers(session, owner) if e.id not in skipped])
        # A job system with filters is asked for the owner's places (Hard pass's work location) and, between full reads,
        # for what is new (docs/PRD.md 39.5, 39.24); the gates then judge everything a board returns, as always.
        gates = engine_gates(await profile_store.current(session, owner))
        # Each part is its own: one that fails is logged, its saved boards kept, and the other still runs, so
        # one failing part does not stop the other.
        read = site_reads.combine(
            await _part(session, "the employers' boards", postings.refresh(session, owner, fetch, only=built_in,
                                                                           gates=gates)),
            await _part(session, "the sites' skills", site_reads.refresh(session, owner, fetch, by_skill)))
        await _recipes_upkeep(session, owner)
        await db.release_if_free(session)
    except Exception as exc:  # planning failed: nothing was read, and the rest of the night still goes on
        log.exception("planning the night's read of the boards failed")
        await _let_go(session)
        read = {**site_reads.combine({}, {}), "faults": [_fault("the night's read of the boards", exc)]}
    finally:
        await fetch.close()
    await where.retire_broken(session, owner, read["faults"])
    # The postings of a board nothing reads are Not checked (docs/PRD.md 39.8), whatever took it out of the read; the
    # ones this read saw are open or closed as they were found (`postings.store`).
    marked = await not_checked.settle(session, owner)
    return {**{k: read[k] for k in ("employers", "failed", "seen", "new", "closed", "reopened", "unread", "cut")},
            "not_checked": marked, "faults": read["faults"][:20]}


def _fault(what: str, exc: Exception) -> dict:
    return {"employer": what, "why": f"{type(exc).__name__}: {exc}"[:500], "kind": "failed"}


async def _let_go(session) -> None:
    """After a part of the read failed: what the session holds that was not saved is dropped (the boards saved before it
    stay), so the next part begins on a clean transaction. Nothing when the session may not be released here."""
    if db.can_release(session):
        try:
            await session.rollback()
        except Exception:
            log.exception("could not roll back after a failed part of the night's read")


async def _part(session, what: str, running) -> dict:
    """One part of the read of the boards: its result, or, when it fails, nothing read and one fault naming it (logged
    with its traceback), so the parts after it still run."""
    try:
        return await running
    except Exception as exc:
        log.exception("reading %s failed; the rest of the night's read goes on", what)
        await _let_go(session)
        return {**site_reads.combine({}, {}), "failed": 1, "faults": [_fault(what, exc)]}


async def read_board_feed(session, instance, data):
    """A board's signed-out job search, when the owner switched it on (PRD path 48): its postings join the same index.
    A step of its own, so a failure in the boards cannot skip it; a failure here is logged and counts nothing."""
    zero = {"seen": 0, "new": 0, "requests": 0, "backoffs": 0, "searches": 0, "searches_read": 0, "skipped": 0,
            "unread": 0, "stopped": None, "mode": None, "details": 0, "lists_read": 0, "lists_missed": 0}
    board = site_registry.board_reader("guest")
    if board is None:  # no reader of a followed board's search here
        return zero
    try:
        linked = await board.read_night(session, data["owner"])
    except Exception:
        log.exception("reading the board's feed failed; the rest of the night's read goes on")
        await _let_go(session)
        return zero
    return {key: linked[key] for key in ("seen", "new", "requests", "backoffs", "searches", "searches_read", "skipped",
                                         "unread", "stopped", "mode", "details", "lists_read",
                                         "lists_missed")}


async def keep_current_step(session, instance, data):
    """The first step of the night's read: stops judged under an older Hard pass than the one in force (or none) are
    judged again, and scores worked out under an older Prioritization (or none) are worked out again, so the night
    begins from a log that is current (docs/PRD.md 39.14, 39.21, 39.23). Code only; it does nothing when every stop and
    score is current. A pass that cannot finish is logged and never stops the night's read: it leaves the rows as they
    were, or, for the re-gate, which commits in slices here (`regate.py`), the slices it finished, and the next start or
    night finds the rest. Inside a savepoint or the engine's call it is one transaction that goes with the savepoint."""
    nested = not db.can_release(session)
    try:
        if nested:
            async with session.begin_nested():
                return await keep_current.run(session, data["owner"])
        return await keep_current.run(session, data["owner"])
    except Exception:
        log.exception("keeping the log current failed; the night's read goes on")
        if not nested:
            await session.rollback()
        return {"gates": None, "scores": None}


async def merge_employers(session, instance, data):
    """Two employer records that the table in `employer_match` says are one employer (a website beside names that look
    alike, a website the owner or the researcher confirmed for both, a job-system board beside names that look the same,
    the same name) are merged. Every other pair is two employers: nobody is asked, and a question an older rule raised
    is closed. Done after the read and
    the following, so what the boards showed is on the records before the layoff notices and the filter look at them
    (docs/PRD.md 36.4, 36.5). A pass that cannot finish is logged and leaves the records as they were: it never stops
    the night's read."""
    try:
        # The look at whether anything is to be done is made on a reader's session, which takes no writer's turn; only
        # a pass that has something to write takes the turn, and it reads everything again there.
        async with off_turn.reading(session) as reader:
            if await employer_pairs.nothing_to_settle(reader, data["owner"]):
                return {"merged": 0}
        async with session.begin_nested():
            return await employer_pairs.settle(session, data["owner"])
    except Exception:
        log.exception("looking for duplicate employer records failed; the night's read goes on")
        return {"merged": 0}


async def layoff_notices(session, instance, data):
    fetch = polite.make()
    try:
        found = await signals.load_warn(session, data["owner"], fetch)
    finally:
        await fetch.close()
    return {"added": found.get("added", 0), "employers": found.get("employers", 0), "error": found.get("error")}


async def sift_postings(session, instance, data):
    """The filter (`sift.py`). Where a posting's list names no place (only a count such as "26 Locations") and its job
    system names the places on the posting's own page, that page is read before the place is judged, within the
    night's descriptions (`details_per_night`); the read goes through the polite fetcher like every other."""
    fetch = polite.make()
    try:
        profile = await profile_store.current(session, data["owner"])
        await db.release_if_free(session)  # the profile read took the writer's turn: the filter's reads take none
        sifted = await sift.sift(session, data["owner"], profile,
                                 fetch=fetch, reads=int(data.get("details_per_night") or 0))
    finally:
        await fetch.close()
    return {"candidates": len(sifted["candidates"]), "stopped": sifted["stopped"], "routes": sifted["routes"],
            "read": sifted["read"]}


async def quiet_roles(session, instance, data):
    """A role whose posting the employer took down, and an Applied role with no word for the owner's number of days,
    move to Gone Quiet (`quiet.py`; docs/PRD.md 14.7, 15.1.8). After the filter, which links a repost to its role, so a
    role does not move on the night its repost first appears. Code only: no Claude and no fetching. A pass that cannot
    finish is logged and leaves the roles as they were: it never stops the night's read."""
    releasing = db.can_release(session)  # not inside a savepoint or a process step: the pass commits as it goes
    try:
        if releasing:
            await db.release(session)  # what the pass leaves unsaved when it fails is rolled back below
            result = await quiet.run(session, data["owner"])
        else:
            async with session.begin_nested():
                result = await quiet.run(session, data["owner"])
    except Exception:
        log.exception("moving the roles that went quiet failed; the night's read goes on")
        if releasing:
            await session.rollback()
        return {"went": 0, "silent": 0, "days": None}
    return {"went": result.went, "silent": result.silent, "days": result.days}


async def read_descriptions(session, instance, data):
    """The text of every open posting the code gates let through and that has none (docs/PRD.md 39.6), read by code
    from its job system with no Claude: the likeliest first, up to the mission's descriptions a night (-1: every one,
    of this set and of no other). The pages the filter read to learn where a posting is (`sifted.read`) came out of
    the night's number. It does not wait on what the filter found tonight: a posting an earlier night passed, or that
    a change to the owner's settings let through since, is in the set until it has its text."""
    read_for_places = int((data.get("sifted") or {}).get("read") or 0)
    left = postings.pages_left(int(data.get("details_per_night") or 0), read_for_places)  # None: no limit (limits.py)
    query = select(JobPosting).where(*postings.needing_text(data["owner"])).order_by(
        JobPosting.fit_prob.desc().nulls_last(), JobPosting.first_seen)
    if left is not None:  # a read taken up after a restart has spent part of the night's number already
        left = max(0, left - await read_progress.details_before())
    # The owed set is a long read (a long read): with no transaction open yet it is made in a
    # reader's session, which takes no writer's turn, and the rows are then taken into `session` for the fills.
    async with off_turn.reading(session) as reader:
        waiting = list((await reader.scalars(query if left is None else query.limit(left))).all())
    session.add_all(waiting)
    await read_progress.set_details_total(session, len(waiting))
    fetch = polite.make()
    filled, reached = 0, 0
    try:
        # The clock is looked at between slices: when the night's time has run out (docs/PRD.md 28.17) the rest are
        # left as they are, for the next read to take first.
        for start in range(0, len(waiting), DETAILS_AT_A_TIME):
            if polite.timeup(fetch):
                break
            batch = waiting[start:start + DETAILS_AT_A_TIME]
            filled += await postings.fill_details(session, fetch, batch)
            reached = start + len(batch)
            await checkpoint.reached()  # a slice's texts are whole: the read saves them here, not at the step's end
            await read_progress.note_details(session, len(batch))
    finally:
        await fetch.close()
    return {"filled": filled, "left": len(waiting) - reached}


def _register() -> None:
    reg = registry.register
    boards_source = "backend/app/jobsearch/postings/index.py"
    reg("jobs.keep_current", keep_current_step, inputs=["owner"], outputs=["gates", "scores"],
        description="Judges the stops that were made under an older Hard pass than the one in force (or none) again, "
                    "and works the scores that were worked out under an older Prioritization (or none) out again: "
                    "the same code a save of those settings runs, for what a release left behind. Does nothing when "
                    "every stop and score is current.",
        source="backend/app/jobsearch/research/keep_current.py")
    reg("jobs.resolve_links", resolve_links, inputs=["owner"], outputs=["found"],
        description="Reads each employer's job system from the links the app holds.",
        source="backend/app/jobsearch/employers/resolve.py")
    reg("jobs.read_boards", read_boards, inputs=["owner"],
        outputs=["employers", "failed", "seen", "new", "closed", "reopened", "unread", "cut", "not_checked", "faults"],
        description="Reads every switched-on site into the posting index, politely: an employer on a known job system "
                    "through its adapter, a site the scout learned through its saved skill. Records each failure. "
                    "Each board is read to its last page; when the night's time runs out, the boards not begun wait "
                    "for the next read and the one being read keeps what it has (and closes nothing). The open "
                    "postings of a board the app no longer reads are marked Not checked.",
        source=boards_source)
    reg("jobs.read_board_feed", read_board_feed, inputs=["owner"],
        outputs=["seen", "new", "requests", "backoffs", "searches", "searches_read", "skipped", "unread", "stopped",
                 "mode", "details", "lists_read", "lists_missed"],
        description="Reads a board's job search into the posting index when the owner switched it on and in the "
                    "owner's off hours; nothing otherwise. Through the app's own Chrome, signed in, when it is up and "
                    "signed in to the board (the page's own answers, else the page; the whole text of each new "
                    "posting), else as a guest, every one of the owner's searches once. It waits out a slow-down, "
                    "skips a search the board cannot answer and ends at a refusal, a security check or repeated "
                    "slow-downs. A step of its own: a failure reading the boards cannot skip it.",
        source="backend/app/jobsearch/actions.py")
    reg("jobs.merge_employers", merge_employers, inputs=["owner"], outputs=["merged"],
        description="Merges the employer records its table says are one employer (a website beside names that look "
                    "alike, a website the owner or the researcher confirmed for both, a job-system board beside names "
                    "that look alike); every other pair stays two employers and nobody is asked.",
        source="backend/app/jobsearch/employers/pairs.py")
    reg("jobs.layoff_notices", layoff_notices, inputs=["owner"], outputs=["added", "employers", "error"],
        description="Matches the recent state layoff notices to known employers as dated layoff events.",
        source="backend/app/jobsearch/employers/signals.py")
    reg("jobs.sift_postings", sift_postings, inputs=["owner", "details_per_night"],
        outputs=["candidates", "stopped", "routes", "read"],
        description="Filters the new postings: the gates that need no judgement stop a posting (a place that is only "
                    "a count of places is read from the posting's own page first, where the job system names them); "
                    "the classifier only changes the order of the ones that passed, the likeliest first, and none is "
                    "hidden or dropped. Every posting "
                    "that passed becomes a role.",
        source="backend/app/jobsearch/gating/sift.py")
    reg("jobs.quiet_roles", quiet_roles, inputs=["owner"], outputs=["went", "silent", "days"],
        description="Moves a role to Gone Quiet when the employer took its posting down, or an Applied role has had no "
                    "word for the owner's number of days. Code only.",
        source="backend/app/jobsearch/pipeline/quiet.py")
    reg("jobs.read_descriptions", read_descriptions, inputs=["owner", "details_per_night", "sifted"],
        outputs=["filled", "left"],
        description="Reads the description of every open posting the code gates let through that has none, from its "
                    "job system, up to the mission's number a night (-1: every one) less the pages the filter read "
                    "for places, until the night's time runs out (the rest are left for the next read).",
        source=boards_source)
    reg("jobs.open_run", open_run, inputs=["owner"], outputs=["gates"],
        description="Reads the owner's hard gates from the stored profile.", source=SOURCE)
    reg("jobs.take_reported", take_reported, inputs=["owner", "run"], outputs=["count", "roles"],
        description="Reads each role the scout reported for what the gates compare (age, place, pay), with the fit its "
                    "answers add up to.",
        source=SOURCE)
    reg("jobs.dedupe", dedupe, inputs=["owner", "run", "taken"], outputs=["roles", "duplicates"],
        description="Sets aside roles already stored for the owner (tracker or an earlier search) and repeats: the "
                    "same company and title written another way, or the same posting, is the same role.",
        source=SOURCE)
    reg("jobs.store", store, inputs=["owner", "run", "fresh", "verdicts"], outputs=["kept", "screened_out"],
        description="Stores every new role: kept as new, or screened out with the reason.", source=SOURCE)
    reg("jobs.follow_postings", follow_postings, inputs=["owner"], outputs=["looked", "followed"],
        description="Looks for the employer's own posting of each role found on a job board among the listings the app "
                    "already reads, and makes the one confident match the role's link, keeping the job board link; "
                    "then tries again, in code, to source every role in play that is not Sourced (`sourcing.sweep`).",
        source="backend/app/jobsearch/employers/follow.py")
    reg("jobs.summarize", summarize, inputs=["run", "reported", "fresh", "stored"], outputs=["text"],
        description="Writes the round's counts and best finds, and closes the search run.", source=SOURCE)
    verdict_out = ["key", "keep", "reason", "note", "fit", "gate"]
    reg("screen.drop_excluded", drop_excluded, inputs=["role"], outputs=verdict_out,
        description="Drops a role at an employer the owner never works for.", source=SOURCE)
    reg("screen.drop_location", drop_location, inputs=["role"], outputs=verdict_out,
        description="Drops a role whose way of working or place is not one the owner allows.", source=SOURCE)
    reg("screen.drop_comp", drop_comp, inputs=["role", "gates"], outputs=verdict_out,
        description="Drops a role whose pay is under the owner's amount.", source=SOURCE)
    reg("screen.drop_stale", drop_stale, inputs=["role", "gates"], outputs=verdict_out,
        description="Drops a posting older than the owner's limit.", source=SOURCE)
    reg("screen.keep", keep, inputs=["role"], outputs=verdict_out,
        description="Keeps a role that passed every gate, with its fit and any note.", source=SOURCE)


_register()
