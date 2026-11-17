"""One role, whole (docs/PRD.md paths 11 and 12): its facts, every event in its history with the reasoning behind each,
and its packets. Read-only; the moves and packets are made elsewhere (board.py, packets.py)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.gating.role_gates import gate_results
from app.jobsearch.interviews import core as interviews
from app.jobsearch.models import ORIGIN_OWNER, ORIGIN_TRACKER, Employer, SearchRun
from app.jobsearch.models import Opportunity as Role
from app.jobsearch.pipeline import board, ended, scout_line
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.postings import cleansed, whole
from app.jobsearch.postings import text as posting_text
from app.jobsearch.profile import documents
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.base import SECTIONS
from app.jobsearch.research import role_summary
from app.jobsearch.work import hours as night
from app.jobsearch.writing import packets
from app.models.work import TrailEvent
from app.timeutil import iso_utc, owner_today


async def detail(session: AsyncSession, owner: str, role_id: uuid.UUID) -> dict:
    role = await session.get(Role, role_id)
    if role is None or role.owner != owner:
        raise board.NoSuchRole("no such role")
    on = owner_today()
    profile = await profile_store.current(session, owner)
    span = scoring.bounds(profile)
    employer = await session.get(Employer, role.employer_id) if role.employer_id else None
    office = (employer.headquarters or (employer.offices or [None])[0]) if employer else None
    # The top of the page is the role's row, the one its column shows (`role_row`); what follows is the page's own.
    facts: dict = dict(await role_row.row(session, owner, role_id))
    facts.update({
        "notes": role.notes, "found": role.found.isoformat() if role.found else None,
        "posted": role.posted.isoformat() if role.posted else None, "board_found_on": role.board, "ats": role.ats,
        "level": role.level, "sector": role.sector, "landed_interview": role.landed_interview,
        # The role's two links: the employer's own posting (none until it is found) and the job board it was found
        # on (none for a role found on the employer's own site). `url` is the one read and opened.
        **employer_posting.links_of(role, await employer_posting.job_board_hosts(session, owner)),
    })
    breakdown = role.fit_breakdown or {}
    # The text the app holds of the posting: the role's own, else a posting of the role's (docs/PRD.md 39.29: the role
    # shows the text that is held, whether or not it was copied onto the role).
    text, read_at = await posting_text.held(session, role)
    facts["description"] = text or None
    facts["description_at"] = iso_utc(read_at) if read_at else None
    # Whose copy the text is, when it is not the posting's own source's: the board's, while the employer's could not be
    # read (the owner, 2026-11-05, the board's copy hook). A posting that holds the employer's text is shown in
    # its place.
    facts["description_from"] = role.description_from if text and text == (role.description or "").strip() else None
    # The pay expectation names the place of work: the role's own, else the employer's headquarters (a guess the
    # expectation may make; the row says only what the role says).
    work = {**facts["work"], "place": facts["work"]["place"] or office}
    return {
        "role": facts,
        "employer": employers.view(employer) if employer else None,
        "scoring": scoring_panel(role, breakdown, profile, span),
        "gates": gate_results(role, profile, on, employer, await cleansed.gate_copy_of(session, owner, role.id),
                              await freshness.posted_of(session, role)),
        # The pay ask worked out at final scoring (docs/PRD.md 25.6), before any packet.
        "final_ask": (role.analysis or {}).get("ask") or None,
        # The three lines at the top of the role's pane (`role_summary`), or None; `summary_askable` says the pane may
        # ask for one (researched, none, not asked yet).
        "summary": role_summary.view(role),
        "summary_askable": role_summary.askable(role),
        "requirements": await requirements(session, owner, role),
        # The posting's preferred items, classed the same way (docs/PRD.md 26.1): shown apart, read-only.
        "preferred": await requirements(session, owner, role, "preferred"),
        # The posting's duties, classed the same way (the owner, 2026-10-16): shown apart, read-only, as preferred is.
        "responsibilities": await requirements(session, owner, role, "responsibilities"),
        "comp_answer": comp_answer(role, profile, work),
        "interviews": await interviews.for_role(session, owner, role.id, comp_answer(role, profile, work)),
        "peak": night.peak_view(profile.settings),
        "moves_to": [{"status": s, "label": board.LABELS[s]} for s in board.MOVES_TO],
        "ended_reasons": ended.offered(),
        "timeline": await timeline(session, owner, role, text_read=not whole.thin(text)),
        "packets": await packets.for_role(session, owner, role.id),
    }


def scoring_panel(role: Role, breakdown: dict, profile, span) -> dict:
    """A role's fit for its panel (docs/PRD.md 30.6). `kind` says how it was scored: `asks` (each of the owner's asks
    with its answer, the points it earned and why, grouped by the part of a posting), `old` (the base score and
    modifiers it had before asks, as it was stored, and its percent on the old scale) or `none` (only a fit)."""
    target = getattr(role, "target_role", None)
    own = span.for_role(target) if span is not None and hasattr(span, "for_role") else span
    out = {"kind": "none", "total": role.fit, "percent": scoring.percent_of(role, span),
           "possible": own[1] if own and own[1] > own[0] else None, "asks": [], "old": None,
           "sections": [{"id": s, "label": label} for s, label in SECTIONS]}
    if scoring.is_asks(breakdown):
        # A score names the points it was worked out against and the version of Prioritization it was worked out under
        # (docs/PRD.md 39.21); a role that waits for its research has answers and no score (39.20).
        against = breakdown.get("possible")
        out.update(kind="asks", total=breakdown.get("total", role.fit), earned=breakdown.get("earned"),
                   asks=_with_graded_items(scoring.view(breakdown, profile, target), role, profile),
                   version=breakdown.get("version"),
                   # How much of what the role could earn has an answer, as the score was worked out (a whole percent).
                   decided=breakdown.get("decided"), coverage=breakdown.get("coverage"), math=breakdown.get("math"),
                   **({"possible": against} if isinstance(against, int | float) and against > 0 else {}))
    elif breakdown:
        out.update(kind="old", old={
            "base": breakdown.get("base"), "base_why": (breakdown.get("base_why") or "").strip(),
            "noted": (breakdown.get("noted") or "").strip(), "total": breakdown.get("total"),
            "applied": breakdown.get("applied") or [], "not_applied": breakdown.get("not_applied") or [],
            # A score that was one line, unpacked part by part: the line, what matched nothing, the old fit.
            "unpacked": breakdown.get("unpacked"),
            "scale": [span.legacy[0], span.legacy[1]] if span is not None and getattr(span, "legacy", None) else None,
        })
    return out


def _with_graded_items(rows: list[dict], role: Role, profile) -> list[dict]:
    """The graded requirements' row with the requirements it
    graded, each with its class and the points the owner's numbers give it, so the page can open the row. The score
    keeps only the counts; the items are the analyst's, in the research record."""
    grade = profile.scoring.grade_requirements
    if grade is None:
        return rows
    found = role.analysis or {}
    out = []
    for row in rows:
        if row.get("kind") != "grade":
            out.append(row)
            continue
        by_required = {"P": grade.required_proven, "T": grade.required_transferable, "U": grade.required_unproven}
        stand_ins = {" ".join(str(it.get("item") or "").lower().split()) for it in found.get("required") or []
                     if isinstance(it, dict) and it.get("from_duties")}
        weights = {"required": by_required, "responsibilities": by_required,
                   "preferred": {"P": grade.preferred_proven_or_transferable,
                                 "T": grade.preferred_proven_or_transferable, "U": grade.preferred_unproven}}
        items = [{"side": side, "item": it.get("item", ""), "class": it["class"], "points": weights[side][it["class"]]}
                 for side in ("required", "responsibilities", "preferred") if row.get(side) is not None
                 for it in found.get(side) or [] if isinstance(it, dict) and it.get("class") in weights[side]
                 and not (side == "responsibilities"
                          and " ".join(str(it.get("item") or "").lower().split()) in stand_ins)]
        out.append({**row, "items": items})
    return out


async def requirements(session: AsyncSession, owner: str, role: Role, section: str = "required") -> list[dict]:
    """The posting's required items as classed (docs/PRD.md 26.1), or its preferred ones or its responsibilities
    (`section`): each with its
    class, who classed it, why, and the master resume lines behind it with their text. A role researched before the
    preferred items were classed has none."""
    items = (role.analysis or {}).get(section) or []
    if not items:
        return []
    text = documents.master_texts((await profile_store.owner_documents(session, owner)).resume)
    return [{"index": i, "item": it.get("item", ""), "class": it.get("class"), "why": it.get("why", ""),
             "line": it.get("line"),
             "group": str(it.get("group") or "").strip(),
             "from_duties": bool(it.get("from_duties")),
             "by": it.get("by", "analyst"), "owner_why": it.get("owner_why", ""),
             "from": [{"id": x, "text": text.get(x, "(no longer in the master)")} for x in it.get("from") or []]}
            for i, it in enumerate(items)]


def comp_answer(role: Role, profile, work: dict) -> dict | None:
    """The expectation as an application asks for it, and its assumptions, from the numbers and the owner's settings
    alone (docs/PRD.md 26.10 and 26.11)."""
    worked = (role.analysis or {}).get("ask")
    if not worked or not isinstance(worked.get("expectation", worked.get("ask")), int):
        return None
    st = profile.settings
    basis = "base salary" if st.comp_basis == "base" else "total compensation"
    amount = worked.get("expectation", worked.get("ask"))
    place = {"remote": "remote", "hybrid": f"hybrid in {work.get('place')}" if work.get("place") else "hybrid",
             "onsite": f"in person in {work.get('place')}" if work.get("place") else "in person"}.get(work.get("mode"))
    assumes = st.comp_assumptions.strip().rstrip(".")
    expectation = f"My starting {basis} expectation is ${amount:,}" + (f", assuming {assumes}." if assumes else ".")
    assumptions = f"This is {basis} for the role as posted" + (f" ({place})" if place else "")
    assumptions += f"; it assumes {assumes}." if assumes else "."
    if worked.get("single_figure"):
        assumptions += " The posting gives one figure, and the expectation follows it."
    return {"expectation": expectation, "assumptions": assumptions}


async def timeline(session: AsyncSession, owner: str, role: Role, *, text_read: bool) -> list[dict]:
    """Everything that happened to the role, newest first: how it arrived, how it was scored and screened, every
    move, every packet asked for and filed, with the words and reasoning each event carried. `text_read` is whether the
    app holds the posting's text: the scout's line beside Scored then leaves out what it said of the text being
    missing (`scout_line.shown`)."""
    events: list[dict] = []
    if role.origin == ORIGIN_TRACKER:
        events.append({"at": iso_utc(role.created_at), "kind": "arrived", "who": "You",
                       "sentence": "Came in from your tracker.", "detail": None})
    elif role.origin == ORIGIN_OWNER:
        events.append({"at": iso_utc(role.created_at), "kind": "arrived", "who": "You",
                       "sentence": "You added it by pasting its link.", "detail": None})
    else:
        run = await session.get(SearchRun, role.source_run) if role.source_run else None
        how = f"Found by a search{f' looking for {run.focus}' if run and run.focus else ''}"
        where = f" on {role.board}" if role.board else ""
        events.append({"at": iso_utc(role.created_at), "kind": "arrived", "who": "Scout",
                       "sentence": f"{how}{where}.", "detail": None})
        if role.fit is not None:
            events.append({"at": iso_utc(role.created_at), "kind": "scored", "who": "Scout",
                           "sentence": f"Scored {role.fit}.",
                           "detail": scout_line.shown(role.fit_evidence, text_read)})
        if role.screened_out_reason:
            events.append({"at": iso_utc(role.created_at), "kind": "screened", "who": "Gates",
                           "sentence": "Screened out.", "detail": role.screened_out_reason})
    rows = (await session.scalars(select(TrailEvent).where(
        TrailEvent.owner == owner, TrailEvent.link_kind == "opportunity", TrailEvent.link_id == role.id))).all()
    events += [{"at": iso_utc(r.at), "kind": r.event, "who": r.who, "sentence": r.sentence, "detail": r.detail}
               for r in rows]
    for p in await packets.for_role(session, owner, role.id):
        if p["written_at"]:
            why = (p["reasoning"] or {}).get("problem")
            events.append({"at": p["written_at"], "kind": "packet_written", "who": "Writer",
                           "sentence": f"Wrote version {p['version']} of the packet.",
                           "detail": f"The problem it answers: {why}" if why else None})
    return sorted(events, key=lambda e: e["at"] or "", reverse=True)
